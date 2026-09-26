"""Unit tests for the webhook-delivery NATS consumer (issue #566).

Covers the ack/nak/DLQ decision taxonomy in `app.services.nats_consumer`:
poison-message routing, max-deliver exhaustion, transient-error retry, the
DLQ-publish swallow-on-failure, and `handle_event`'s per-target exception
isolation. All existing webhook delivery tests (`test_webhooks.py`) call
`deliver_one` directly and bypass the consumer entirely; this file targets the
consumer's own control flow with a stubbed `js` (JetStream) and fake message.

The trailing "health consumer config" and "health event routing" sections
(issue #831) pin the second HERD_HEALTH durable consumer's constants and prove
`handle_event` fans a `device.health_transition` payload out to a subscription
that lists it and not to one that lists only reservation events, and that a
poison message on the health subject routes to the health DLQ subject.
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
    num_delivered = nats_consumer.NATS_MAX_DELIVER - 1
    payload = _payload()
    msg = _FakeMsg(payload, num_delivered=num_delivered)
    js = AsyncMock()

    async def _handler(event_data, raw_body, session_factory, dedupe_key):
        raise RuntimeError("transient")

    result = await nats_consumer.process_message(msg, js, _handler, session_factory=object())

    assert result == "nak"
    # issue #895: the NAK must carry an explicit delay (schedule[num_delivered - 1]),
    # never a bare msg.nak(), which JetStream redelivers immediately.
    expected_delay = nats_consumer.NATS_NAK_BACKOFF_SECONDS[
        min(num_delivered - 1, len(nats_consumer.NATS_NAK_BACKOFF_SECONDS) - 1)
    ]
    msg.nak.assert_awaited_once_with(delay=expected_delay)
    msg.ack.assert_not_awaited()
    js.publish.assert_not_awaited()


async def test_process_message_nak_on_transient_error_first_delivery():
    payload = _payload()
    msg = _FakeMsg(payload, num_delivered=1)
    js = AsyncMock()

    async def _handler(event_data, raw_body, session_factory, dedupe_key):
        raise RuntimeError("transient")

    result = await nats_consumer.process_message(msg, js, _handler, session_factory=object())

    assert result == "nak"
    msg.nak.assert_awaited_once_with(delay=nats_consumer.NATS_NAK_BACKOFF_SECONDS[0])


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


# --- health consumer config (issue #831) ------------------------------------


def test_health_consumer_config_pinned():
    """Pins the HERD_HEALTH consumer's constants: its own stream, subject
    filter, durable name, and DLQ subject, distinct from the reservations
    consumer's, plus the batch == 1 rule (issue #648) which is enforced by the
    single shared `_consumer_loop` closure calling `psub.fetch(1, ...)`
    regardless of which subscription it belongs to (see
    test_consumer_loop_fetches_one_message_at_a_time in
    test_nats_consumer_lifecycle.py, which proves this for both
    subscriptions)."""
    assert nats_consumer.HEALTH_STREAM == "HERD_HEALTH"
    assert nats_consumer.HEALTH_SUBJECT_PATTERN == "herd.health.*"
    assert nats_consumer.HEALTH_DURABLE == "integration-webhooks-health-consumer"
    # 4-token DLQ subject, deliberately outside the 3-token consumer filter.
    assert nats_consumer.HEALTH_DLQ_SUBJECT == "herd.health.dlq.integration"
    assert nats_consumer.HEALTH_DLQ_SUBJECT != nats_consumer.NATS_DLQ_SUBJECT
    assert nats_consumer.HEALTH_DURABLE != nats_consumer.NATS_DURABLE
    assert nats_consumer.HEALTH_STREAM != nats_consumer.NATS_STREAM


async def test_process_message_poison_health_message_routes_to_health_dlq():
    """A poison message processed with the health subscription's dlq_subject
    routes to herd.health.dlq.integration, not the reservations DLQ."""
    msg = _FakeMsg(b"not-json")
    js = AsyncMock()

    async def _handler(event_data, raw_body, session_factory, dedupe_key):
        raise AssertionError("handler should not be called on a poison message")

    result = await nats_consumer.process_message(
        msg,
        js,
        _handler,
        session_factory=object(),
        dlq_subject=nats_consumer.HEALTH_DLQ_SUBJECT,
    )

    assert result == "dlq"
    js.publish.assert_awaited_once_with(nats_consumer.HEALTH_DLQ_SUBJECT, msg.data)
    msg.ack.assert_awaited_once()


async def test_process_message_max_deliver_exhausted_health_routes_to_health_dlq():
    """Same exhaustion path as the reservations DLQ test, but with the health
    subscription's dlq_subject; the DLQ subject actually used is the one
    passed in, not the module-level default."""
    payload = _payload(event="device.health_transition", device_id=str(uuid.uuid4()))
    msg = _FakeMsg(payload, num_delivered=nats_consumer.NATS_MAX_DELIVER)
    js = AsyncMock()

    async def _handler(event_data, raw_body, session_factory, dedupe_key):
        raise RuntimeError("still failing")

    result = await nats_consumer.process_message(
        msg,
        js,
        _handler,
        session_factory=object(),
        dlq_subject=nats_consumer.HEALTH_DLQ_SUBJECT,
    )

    assert result == "dlq"
    js.publish.assert_awaited_once_with(nats_consumer.HEALTH_DLQ_SUBJECT, payload)


# --- health event routing through handle_event (issue #831) -----------------


async def test_handle_event_routes_health_transition_to_matching_subscription(monkeypatch):
    """A subscription whose event_types lists device.health_transition receives
    the health payload; handle_event needs no health-specific branch since both
    reservation and health payloads carry the event name under the same
    `event` key."""
    target = Target(id=uuid.uuid4(), target_url="https://ok.example", secret="s1")

    async def _load(session_factory, event_name):
        assert event_name == "device.health_transition"
        return [target]

    delivered_to = []

    async def _deliver_one(session_factory, tgt, body, event_id, event_type, *, timeout, attempts):
        delivered_to.append(tgt.id)
        return "delivered"

    monkeypatch.setattr(nats_consumer, "load_matching_targets", _load)
    monkeypatch.setattr(nats_consumer, "deliver_one", _deliver_one)

    device_id = str(uuid.uuid4())
    raw_body = _payload(event="device.health_transition", device_id=device_id)
    await nats_consumer.handle_event(
        json.loads(raw_body), raw_body, session_factory=object(), dedupe_key="health-dk-1"
    )

    assert delivered_to == [target.id]


async def test_handle_event_health_transition_not_delivered_to_reservation_only_subscription():
    """A subscription registered for reservation events only (never having
    matched device.health_transition in load_matching_targets, since matching
    is a plain membership check against event_types) gets nothing for a health
    event. This exercises load_matching_targets' real filtering, not a stub,
    against an in-memory subscription set."""
    from app.services.delivery import load_matching_targets

    class _FakeSub:
        def __init__(self, event_types):
            self.id = uuid.uuid4()
            self.target_url = "https://ok.example"
            self.secret = "s1"
            self.event_types = event_types
            self.is_active = True

    class _FakeScalars:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return self._rows

    class _FakeResult:
        def __init__(self, rows):
            self._rows = rows

        def scalars(self):
            return _FakeScalars(self._rows)

    reservation_only = _FakeSub(["reservation.created", "reservation.failed"])
    health_sub = _FakeSub(["device.health_transition"])

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def execute(self, stmt):
            return _FakeResult([reservation_only, health_sub])

    def _session_factory():
        return _FakeSession()

    targets = await load_matching_targets(_session_factory, "device.health_transition")

    assert [t.id for t in targets] == [health_sub.id]
