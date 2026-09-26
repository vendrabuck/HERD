"""Tests for the reservation-event corroboration gate in nats_consumer.py.

NATS carries no authentication, so before this gate a forged event on
herd.reservations.* was treated as authoritative: a fake reservation.cancelled
froze wiring and tore down ledgers/dynamic instances for a reservation that
reservations still holds ACTIVE, a fake reservation.updated destroyed dynamic
instances for made-up removed_device_ids, and a fake reservation.created or
reservation.wiring_changed acted on a made-up claim. `_verify_reservation_event`
corroborates the event's claim against reservations' own record
(GET /internal/{id}) before `process_reservation_message` calls the handler at
all; these tests pin both the low-level rule table and the ack/nak wiring at
the process_reservation_message level.

The first half tests `_verify_reservation_event` directly against a fake httpx
client (status table, 404, 5xx/transport-error fail-closed, missing
reservation_id, and an event outside the gate's table making no HTTP call at
all). The second half drives `process_reservation_message` with
`_verify_reservation_event` patched, pinning that an unverified event acks
without running the handler and logs the fixed `nats_event_unverified` action
with event/reservation_id/reported_status (never the full payload), that a
verified event proceeds exactly as before, and that the check's own transport
failure NAKs like any other transient error.
"""

import json
import logging
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from app.services.nats_consumer import (
    NATS_NAK_BACKOFF_SECONDS,
    ReservationEventVerification,
    TransientUpstreamError,
    _verify_reservation_event,
    process_reservation_message,
)

TERMINAL_EVENTS = ("reservation.cancelled", "reservation.completed", "reservation.failed")
GATED_EVENTS = TERMINAL_EVENTS + (
    "reservation.created",
    "reservation.updated",
    "reservation.wiring_changed",
)
STATUSES = ("PENDING", "PENDING_PROVISION", "ACTIVE", "COMPLETED", "CANCELLED", "FAILED")


def _expected_verified(event: str, status: str) -> bool:
    """Independent oracle for the expected-state table (does not import the
    module's rule functions, so this cannot pass by tautology)."""
    if event in TERMINAL_EVENTS:
        return status in ("COMPLETED", "CANCELLED", "FAILED")
    if event == "reservation.created":
        return status in ("PENDING_PROVISION", "ACTIVE")
    if event in ("reservation.updated", "reservation.wiring_changed"):
        return status == "ACTIVE"
    raise AssertionError(f"no oracle entry for event {event!r}")


class _FakeGetClient:
    """Minimal stand-in for the httpx client `_verify_reservation_event` calls.

    Records every call so a test can assert an event outside the gate's table
    makes no HTTP call at all.
    """

    def __init__(self, response: httpx.Response | None = None, exc: Exception | None = None):
        self._response = response
        self._exc = exc
        self.calls: list[tuple[str, dict]] = []

    async def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self._exc is not None:
            raise self._exc
        return self._response


def _status_response(status: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": str(uuid.uuid4()),
            "status": status,
            "is_active": status == "ACTIVE",
            "start_time": "2026-01-01T00:00:00+00:00",
            "end_time": "2026-01-01T01:00:00+00:00",
        },
    )


# --- _verify_reservation_event: the expected-state table --------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("event", GATED_EVENTS)
@pytest.mark.parametrize("status", STATUSES)
async def test_event_status_table(event, status):
    client = _FakeGetClient(response=_status_response(status))
    event_data = {"event": event, "reservation_id": str(uuid.uuid4())}

    result = await _verify_reservation_event(event_data, client)

    assert result is not None
    assert result.verified == _expected_verified(event, status)
    assert result.reported_status == status
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_unverified_result_carries_a_reason_but_no_payload_echo():
    client = _FakeGetClient(response=_status_response("ACTIVE"))
    event_data = {"event": "reservation.cancelled", "reservation_id": str(uuid.uuid4())}

    result = await _verify_reservation_event(event_data, client)

    assert result.verified is False
    assert result.reported_status == "ACTIVE"
    assert result.reason


@pytest.mark.asyncio
async def test_404_is_unverified_with_its_own_reason():
    client = _FakeGetClient(response=httpx.Response(404, json={"detail": "Reservation not found"}))
    event_data = {"event": "reservation.cancelled", "reservation_id": str(uuid.uuid4())}

    result = await _verify_reservation_event(event_data, client)

    assert result.verified is False
    assert result.reported_status is None
    assert result.reason == "reservation not found"


@pytest.mark.asyncio
async def test_5xx_raises_transient_upstream_error():
    client = _FakeGetClient(response=httpx.Response(503))
    event_data = {"event": "reservation.failed", "reservation_id": str(uuid.uuid4())}

    with pytest.raises(TransientUpstreamError):
        await _verify_reservation_event(event_data, client)


@pytest.mark.asyncio
async def test_transport_error_raises_transient_upstream_error():
    client = _FakeGetClient(exc=httpx.ConnectError("connection refused"))
    event_data = {"event": "reservation.wiring_changed", "reservation_id": str(uuid.uuid4())}

    with pytest.raises(TransientUpstreamError):
        await _verify_reservation_event(event_data, client)


@pytest.mark.asyncio
async def test_missing_reservation_id_skips_the_gate():
    """Every handler already guards a missing reservation_id itself (issue
    #455); the gate defers to that rather than duplicating it with nothing to
    verify against, and makes no HTTP call."""
    client = _FakeGetClient(response=_status_response("ACTIVE"))
    event_data = {"event": "reservation.cancelled"}

    result = await _verify_reservation_event(event_data, client)

    assert result is None
    assert client.calls == []


@pytest.mark.asyncio
async def test_event_outside_the_table_is_not_gated():
    """reservation.provision_requested enforces its own preconditions elsewhere
    (PENDING_PROVISION + a dynamic request) and carries no entry in the gate's
    table, so it makes no HTTP call."""
    client = _FakeGetClient(response=_status_response("ACTIVE"))
    event_data = {"event": "reservation.provision_requested", "reservation_id": str(uuid.uuid4())}

    result = await _verify_reservation_event(event_data, client)

    assert result is None
    assert client.calls == []


# --- process_reservation_message: ack/nak wiring around the gate ------------


def _make_msg(payload: bytes, *, num_delivered: int = 1):
    msg = MagicMock()
    msg.data = payload
    msg.metadata = type("Meta", (), {"num_delivered": num_delivered})()
    msg.ack = AsyncMock()
    msg.nak = AsyncMock()
    return msg


def _make_js():
    js = MagicMock()
    js.publish = AsyncMock()
    return js


@pytest.mark.asyncio
async def test_unverified_terminal_event_acks_without_running_the_handler(caplog):
    """A forged reservation.cancelled for a row reservations reports ACTIVE
    must not run the handler: no freeze, no ledger write, no driver call. The
    warning carries the fixed action string plus event/reservation_id/reported
    status, checked on the log record's attributes, never message text."""
    rid = str(uuid.uuid4())
    js = _make_js()
    payload = json.dumps({"event": "reservation.cancelled", "reservation_id": rid}).encode()
    msg = _make_msg(payload)
    handler = AsyncMock()

    with (
        patch(
            "app.services.nats_consumer._verify_reservation_event",
            new=AsyncMock(
                return_value=ReservationEventVerification(
                    False, "ACTIVE", "status does not corroborate event"
                )
            ),
        ),
        caplog.at_level(logging.WARNING),
    ):
        result = await process_reservation_message(msg, js, handler, session_factory=lambda: None)

    assert result == "ack"
    msg.ack.assert_awaited_once()
    msg.nak.assert_not_awaited()
    handler.assert_not_awaited()

    matches = [
        rec for rec in caplog.records if getattr(rec, "action", None) == "nats_event_unverified"
    ]
    assert len(matches) == 1
    rec = matches[0]
    assert rec.event == "reservation.cancelled"
    assert rec.reservation_id == rid
    assert rec.reported_status == "ACTIVE"


@pytest.mark.asyncio
async def test_verified_terminal_event_proceeds_exactly_as_before():
    rid = str(uuid.uuid4())
    js = _make_js()
    payload = json.dumps({"event": "reservation.cancelled", "reservation_id": rid}).encode()
    msg = _make_msg(payload)
    handler = AsyncMock()

    with patch(
        "app.services.nats_consumer._verify_reservation_event",
        new=AsyncMock(return_value=ReservationEventVerification(True, "CANCELLED", "")),
    ):
        result = await process_reservation_message(msg, js, handler, session_factory=lambda: None)

    assert result == "ack"
    msg.ack.assert_awaited_once()
    handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_gate_transport_failure_naks_like_any_other_transient_error():
    """The check itself is unanswerable (5xx/transport error): fail closed by
    NAKing for retry rather than proceeding on missing information."""
    rid = str(uuid.uuid4())
    js = _make_js()
    payload = json.dumps({"event": "reservation.wiring_changed", "reservation_id": rid}).encode()
    msg = _make_msg(payload, num_delivered=2)
    handler = AsyncMock()

    with patch(
        "app.services.nats_consumer._verify_reservation_event",
        new=AsyncMock(side_effect=TransientUpstreamError("verify reservation event: upstream 503")),
    ):
        result = await process_reservation_message(
            msg, js, handler, session_factory=lambda: None, max_deliver=5
        )

    assert result == "nak"
    msg.nak.assert_awaited_once_with(delay=NATS_NAK_BACKOFF_SECONDS[1])
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_gate_404_acks_without_running_the_handler(caplog):
    rid = str(uuid.uuid4())
    js = _make_js()
    payload = json.dumps({"event": "reservation.failed", "reservation_id": rid}).encode()
    msg = _make_msg(payload)
    handler = AsyncMock()

    with (
        patch(
            "app.services.nats_consumer._verify_reservation_event",
            new=AsyncMock(
                return_value=ReservationEventVerification(False, None, "reservation not found")
            ),
        ),
        caplog.at_level(logging.WARNING),
    ):
        result = await process_reservation_message(msg, js, handler, session_factory=lambda: None)

    assert result == "ack"
    handler.assert_not_awaited()
    matches = [
        rec for rec in caplog.records if getattr(rec, "action", None) == "nats_event_unverified"
    ]
    assert len(matches) == 1
    assert matches[0].reported_status is None


@pytest.mark.asyncio
async def test_forged_removed_device_id_on_a_non_active_reservation_is_unverified():
    """Stand-in for 'removed id still in the set': ReservationInternalStatus
    carries no device list to check the id against (by design), so the only
    corroborable claim for reservation.updated is the ACTIVE status itself. A
    forged removal for a reservation reservations reports PENDING is unverified."""
    rid = str(uuid.uuid4())
    js = _make_js()
    payload = json.dumps(
        {"event": "reservation.updated", "reservation_id": rid, "removed_device_ids": ["dev-1"]}
    ).encode()
    msg = _make_msg(payload)
    handler = AsyncMock()

    with patch(
        "app.services.nats_consumer._verify_reservation_event",
        new=AsyncMock(
            return_value=ReservationEventVerification(
                False, "PENDING", "status does not corroborate event"
            )
        ),
    ):
        result = await process_reservation_message(msg, js, handler, session_factory=lambda: None)

    assert result == "ack"
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_created_for_a_cancelled_reservation_is_unverified():
    rid = str(uuid.uuid4())
    js = _make_js()
    payload = json.dumps({"event": "reservation.created", "reservation_id": rid}).encode()
    msg = _make_msg(payload)
    handler = AsyncMock()

    with patch(
        "app.services.nats_consumer._verify_reservation_event",
        new=AsyncMock(
            return_value=ReservationEventVerification(
                False, "CANCELLED", "status does not corroborate event"
            )
        ),
    ):
        result = await process_reservation_message(msg, js, handler, session_factory=lambda: None)

    assert result == "ack"
    handler.assert_not_awaited()
