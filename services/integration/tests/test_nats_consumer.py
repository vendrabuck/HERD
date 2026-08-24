"""Unit tests for the webhook-delivery NATS consumer (issue #566).

Covers the ack/nak/DLQ decision taxonomy in `app.services.nats_consumer`:
poison-message routing, max-deliver exhaustion, transient-error retry, the
DLQ-publish swallow-on-failure, and `handle_event`'s per-target exception
isolation. All existing webhook delivery tests (`test_webhooks.py`) call
`deliver_one` directly and bypass the consumer entirely; this file targets the
consumer's own control flow with a stubbed `js` (JetStream) and fake message.
"""

import json
import uuid
from unittest.mock import AsyncMock

from app.services import nats_consumer
from app.services.delivery import Target


class _FakeMsg:
    def __init__(self, data: bytes, num_delivered: int = 1):
        self.data = data
        self.metadata = type("M", (), {"num_delivered": num_delivered, "sequence": None})
        self.ack = AsyncMock()
        self.nak = AsyncMock()


def _payload(event: str = "reservation.created", **extra) -> bytes:
    body = {"event": event, "reservation_id": str(uuid.uuid4())}
    body.update(extra)
    return json.dumps(body).encode()


# --- process_message: poison / max-deliver / transient taxonomy -----------


async def test_process_message_poison_goes_to_dlq():
    msg = _FakeMsg(b"not-json")
    js = AsyncMock()

    async def _handler(event_data, raw_body, session_factory, dedupe_key):
        raise AssertionError("handler should not be called on a poison message")

    result = await nats_consumer.process_message(msg, js, _handler, session_factory=object())

    assert result == "dlq"
    js.publish.assert_awaited_once_with(nats_consumer.NATS_DLQ_SUBJECT, msg.data)
    msg.ack.assert_awaited_once()
    msg.nak.assert_not_awaited()


async def test_process_message_dlq_on_max_deliver_exhausted():
    payload = _payload()
    msg = _FakeMsg(payload, num_delivered=nats_consumer.NATS_MAX_DELIVER)
    js = AsyncMock()

    async def _handler(event_data, raw_body, session_factory, dedupe_key):
        raise RuntimeError("still failing")

    result = await nats_consumer.process_message(msg, js, _handler, session_factory=object())

    assert result == "dlq"
    js.publish.assert_awaited_once_with(nats_consumer.NATS_DLQ_SUBJECT, payload)
    msg.ack.assert_awaited_once()
    msg.nak.assert_not_awaited()


async def test_process_message_nak_on_transient_error():
    payload = _payload()
    msg = _FakeMsg(payload, num_delivered=nats_consumer.NATS_MAX_DELIVER - 1)
    js = AsyncMock()

    async def _handler(event_data, raw_body, session_factory, dedupe_key):
        raise RuntimeError("transient")

    result = await nats_consumer.process_message(msg, js, _handler, session_factory=object())

    assert result == "nak"
    msg.nak.assert_awaited_once()
    msg.ack.assert_not_awaited()
    js.publish.assert_not_awaited()


async def test_process_message_acks_on_success():
    payload = _payload()
    msg = _FakeMsg(payload)
    js = AsyncMock()
    seen = {}

    async def _handler(event_data, raw_body, session_factory, dedupe_key):
        seen["event_data"] = event_data
        seen["raw_body"] = raw_body

    result = await nats_consumer.process_message(msg, js, _handler, session_factory=object())

    assert result == "ack"
    msg.ack.assert_awaited_once()
    msg.nak.assert_not_awaited()
    js.publish.assert_not_awaited()
    assert seen["event_data"]["event"] == "reservation.created"
    assert seen["raw_body"] == payload


async def test_process_message_dlq_publish_failure_does_not_propagate():
    """If the DLQ publish itself fails, the consumer still acks so the pull
    loop keeps draining rather than redelivering the same poison message."""
    msg = _FakeMsg(b"still-not-json")
    js = AsyncMock()
    js.publish.side_effect = RuntimeError("nats down")

    async def _handler(event_data, raw_body, session_factory, dedupe_key):
        raise AssertionError("handler should not be called on a poison message")

    result = await nats_consumer.process_message(msg, js, _handler, session_factory=object())

    assert result == "dlq"
    js.publish.assert_awaited_once()
    msg.ack.assert_awaited_once()
    msg.nak.assert_not_awaited()


async def test_process_message_dlq_publish_failure_on_exhaustion_still_acks():
    """Same swallow-on-failure guarantee on the max-deliver exhaustion path."""
    payload = _payload()
    msg = _FakeMsg(payload, num_delivered=nats_consumer.NATS_MAX_DELIVER)
    js = AsyncMock()
    js.publish.side_effect = RuntimeError("nats down")

    async def _handler(event_data, raw_body, session_factory, dedupe_key):
        raise RuntimeError("still failing")

    result = await nats_consumer.process_message(msg, js, _handler, session_factory=object())

    assert result == "dlq"
    js.publish.assert_awaited_once()
    msg.ack.assert_awaited_once()
    msg.nak.assert_not_awaited()


# --- handle_event: target fan-out and per-target isolation -----------------


async def test_handle_event_no_targets_is_noop(monkeypatch):
    async def _no_targets(session_factory, event_name):
        return []

    deliver_calls = []

    async def _deliver_one(*args, **kwargs):
        deliver_calls.append((args, kwargs))
        return "delivered"

    monkeypatch.setattr(nats_consumer, "load_matching_targets", _no_targets)
    monkeypatch.setattr(nats_consumer, "deliver_one", _deliver_one)

    await nats_consumer.handle_event(
        {"event": "reservation.created"}, b"{}", session_factory=object(), dedupe_key="k1"
    )

    assert deliver_calls == []


async def test_handle_event_missing_event_field_is_noop(monkeypatch):
    """No `event` key means nothing to match against; the fan-out is skipped
    entirely, not attempted against an empty/undefined event name."""
    called = {"load": False}

    async def _load(session_factory, event_name):
        called["load"] = True
        return []

    monkeypatch.setattr(nats_consumer, "load_matching_targets", _load)

    await nats_consumer.handle_event({}, b"{}", session_factory=object(), dedupe_key="k1")

    assert called["load"] is False


async def test_handle_event_swallows_unexpected_delivery_exception(monkeypatch):
    """One target's delivery raising an unexpected error is logged and
    swallowed; the other targets still get delivered to, and the event as a
    whole never NAKs the NATS message over one bad ledger write."""
    target_ok = Target(id=uuid.uuid4(), target_url="https://ok.example", secret="s1")
    target_bad = Target(id=uuid.uuid4(), target_url="https://bad.example", secret="s2")

    async def _load(session_factory, event_name):
        return [target_ok, target_bad]

    delivered_to = []

    async def _deliver_one(
        session_factory, target, body, event_id, event_type, *, timeout, attempts
    ):
        if target is target_bad:
            raise RuntimeError("db unreachable mid-delivery")
        delivered_to.append(target.id)
        return "delivered"

    monkeypatch.setattr(nats_consumer, "load_matching_targets", _load)
    monkeypatch.setattr(nats_consumer, "deliver_one", _deliver_one)

    # Must not raise despite target_bad's exception.
    await nats_consumer.handle_event(
        {"event": "reservation.created"}, b"{}", session_factory=object(), dedupe_key="k1"
    )

    assert delivered_to == [target_ok.id]


async def test_handle_event_calls_deliver_one_with_expected_args(monkeypatch):
    target = Target(id=uuid.uuid4(), target_url="https://ok.example", secret="s1")

    async def _load(session_factory, event_name):
        assert event_name == "reservation.created"
        return [target]

    calls = []

    async def _deliver_one(session_factory, tgt, body, event_id, event_type, *, timeout, attempts):
        calls.append(
            {
                "target": tgt,
                "body": body,
                "event_id": event_id,
                "event_type": event_type,
                "timeout": timeout,
                "attempts": attempts,
            }
        )
        return "delivered"

    monkeypatch.setattr(nats_consumer, "load_matching_targets", _load)
    monkeypatch.setattr(nats_consumer, "deliver_one", _deliver_one)

    raw_body = b'{"event":"reservation.created"}'
    await nats_consumer.handle_event(
        {"event": "reservation.created"}, raw_body, session_factory=object(), dedupe_key="dk-1"
    )

    assert len(calls) == 1
    call = calls[0]
    assert call["target"] is target
    assert call["body"] == raw_body
    assert call["event_id"] == "dk-1"
    assert call["event_type"] == "reservation.created"
