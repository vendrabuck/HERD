"""Tests for nats_consumer.py: reservation-event dispatch after ADR 0009 phase 7,
the process_reservation_message DLQ/retry semantics, the fetch helpers, and the
outbox dedupe-key resolution.

Phase 7 retired the legacy device-set wiring resolvers/executors
(_resolve_l1/l2/l3_switch_operations, _execute_switch_operations,
_execute_l2/l3_switch_operations, _assign_vlans_to_operations, the nats_consumer
_derive_vlan_id copy, EVENT_ACTIONS/L2_EVENT_ACTIONS/L3_EVENT_ACTIONS, and
_fetch_connections_for_device), so their tests were deleted. All wiring is now
fork-driven through reservation.wiring_changed; that reconcile is covered by
test_nats_consumer_wiring_changed.py plus the L2/L3 reconcile suites. What remains
here is dispatch (which non-wiring roles reservation.created/updated retain, and
the terminal ledger-driven teardown), plus the surviving process-message, fetch,
and dedupe-key coverage.
"""

import json
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from app.services.nats_consumer import (
    NATS_DLQ_SUBJECT,
    NATS_MAX_DELIVER,
    PermanentEventError,
    TransientUpstreamError,
    _fetch_device,
    _fetch_template,
    handle_reservation_event,
    process_reservation_message,
)


@asynccontextmanager
async def _noop_db_session():
    yield AsyncMock()


def _noop_get_db_session():
    """Protocol-correct get_db_session stand-in yielding a mock session.

    The terminal-event teardown and the wiring-state freeze (ADR 0007 Decision 7,
    issue #345) open a session on reservation.cancelled/completed/failed, so
    get_db_session must be a real async context manager (mock-backed) rather than a
    bare AsyncMock, which would leak an un-awaited coroutine.
    """
    return _noop_db_session()


# --- ADR 0009 phase 7: retired-symbol tombstones ---


def test_retired_wiring_symbols_removed():
    """Phase 7 deleted the legacy device-set resolvers/executors and their action
    maps from nats_consumer, plus action_succeeded_for_reservation from
    execution_service. Pin their absence so a reintroduction is caught in review."""
    from app.services import execution_service, nats_consumer

    for name in (
        "EVENT_ACTIONS",
        "L2_EVENT_ACTIONS",
        "L3_EVENT_ACTIONS",
        "_resolve_l1_switch_operations",
        "_resolve_l2_switch_operations",
        "_resolve_l3_switch_operations",
        "_execute_switch_operations",
        "_execute_l2_switch_operations",
        "_execute_l3_switch_operations",
        "_assign_vlans_to_operations",
        "_fetch_connections_for_device",
    ):
        assert not hasattr(nats_consumer, name), f"{name} should be retired from nats_consumer"

    assert not hasattr(execution_service, "action_succeeded_for_reservation")


# --- handle_reservation_event dispatch ---


@pytest.mark.asyncio
async def test_handle_unknown_event():
    """Unknown event types warn and return without touching wiring or health tiers."""
    event_data = {"event": "reservation.unknown", "device_ids": []}
    with patch(
        "app.services.nats_consumer.apply_reservation_event_tiers",
        new_callable=AsyncMock,
    ) as tiers:
        await handle_reservation_event(event_data, _noop_get_db_session)
    tiers.assert_not_awaited()


@pytest.mark.asyncio
async def test_created_drives_only_health_tiers_no_wiring():
    """ADR 0009 phase 7: reservation.created applies the health-tier transition and
    NOTHING else. No fork reconcile, no ledger teardown, no dynamic teardown, and no
    wiring-state freeze (a freshly activated reservation must stay reconcilable);
    initial wiring is provisioned by the activation-staged reservation.wiring_changed."""
    event_data = {
        "event": "reservation.created",
        "reservation_id": str(uuid.uuid4()),
        "user_id": str(uuid.uuid4()),
        "device_ids": ["device-1", "device-2"],
    }
    with (
        patch(
            "app.services.nats_consumer.apply_reservation_event_tiers",
            new_callable=AsyncMock,
        ) as tiers,
        patch(
            "app.services.nats_consumer._teardown_from_ledgers",
            new_callable=AsyncMock,
        ) as teardown,
        patch(
            "app.services.nats_consumer._execute_dynamic_teardown",
            new_callable=AsyncMock,
        ) as dyn,
        patch(
            "app.services.nats_consumer.handle_wiring_changed",
            new_callable=AsyncMock,
        ) as wiring,
        patch(
            "app.services.l1_assignment_service.freeze_reservation_wiring",
            new_callable=AsyncMock,
        ) as freeze,
    ):
        await handle_reservation_event(event_data, _noop_get_db_session)

    tiers.assert_awaited_once()
    teardown.assert_not_awaited()
    dyn.assert_not_awaited()
    wiring.assert_not_awaited()
    freeze.assert_not_awaited()


@pytest.mark.asyncio
async def test_updated_removed_devices_drive_dynamic_teardown_only():
    """reservation.updated with removed_device_ids drives dynamic-instance teardown for
    exactly those devices plus the health-tier transition, and NO wiring: a departed
    device's physical wiring is released through the fork, not here (Decision 6)."""
    rid = str(uuid.uuid4())
    uid = str(uuid.uuid4())
    removed = [str(uuid.uuid4()), str(uuid.uuid4())]
    event_data = {
        "event": "reservation.updated",
        "reservation_id": rid,
        "user_id": uid,
        "device_ids": ["device-1"],
        "added_device_ids": [],
        "removed_device_ids": removed,
    }
    with (
        patch(
            "app.services.nats_consumer.apply_reservation_event_tiers",
            new_callable=AsyncMock,
        ) as tiers,
        patch(
            "app.services.nats_consumer._teardown_from_ledgers",
            new_callable=AsyncMock,
        ) as teardown,
        patch(
            "app.services.nats_consumer._execute_dynamic_teardown",
            new_callable=AsyncMock,
        ) as dyn,
        patch(
            "app.services.nats_consumer.handle_wiring_changed",
            new_callable=AsyncMock,
        ) as wiring,
    ):
        await handle_reservation_event(event_data, _noop_get_db_session)

    tiers.assert_awaited_once()
    dyn.assert_awaited_once()
    assert dyn.await_args.kwargs["removed_device_ids"] == removed
    teardown.assert_not_awaited()
    wiring.assert_not_awaited()


@pytest.mark.asyncio
async def test_updated_added_only_drives_no_teardown_no_wiring():
    """reservation.updated that only ADDS devices drives no dynamic teardown and no
    wiring: an added device wires nothing until a fork save draws its connections.
    Only the health-tier transition runs."""
    event_data = {
        "event": "reservation.updated",
        "reservation_id": str(uuid.uuid4()),
        "user_id": str(uuid.uuid4()),
        "device_ids": ["device-1", "device-2"],
        "added_device_ids": ["device-2"],
        "removed_device_ids": [],
    }
    with (
        patch(
            "app.services.nats_consumer.apply_reservation_event_tiers",
            new_callable=AsyncMock,
        ) as tiers,
        patch(
            "app.services.nats_consumer._teardown_from_ledgers",
            new_callable=AsyncMock,
        ) as teardown,
        patch(
            "app.services.nats_consumer._execute_dynamic_teardown",
            new_callable=AsyncMock,
        ) as dyn,
        patch(
            "app.services.nats_consumer.handle_wiring_changed",
            new_callable=AsyncMock,
        ) as wiring,
    ):
        await handle_reservation_event(event_data, _noop_get_db_session)

    tiers.assert_awaited_once()
    dyn.assert_not_awaited()
    teardown.assert_not_awaited()
    wiring.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_cancelled_event_dispatches_ledger_teardown():
    """A terminal event (reservation.cancelled) freezes the wiring state FIRST (issue
    #461), then dispatches the ledger-driven teardown, then the dynamic-instance
    teardown. Wiring comes from the three ledgers via _teardown_from_ledgers, never a
    device-set resolver (retired)."""
    rid = str(uuid.uuid4())
    uid = str(uuid.uuid4())
    event_data = {
        "event": "reservation.cancelled",
        "reservation_id": rid,
        "user_id": uid,
        "device_ids": ["device-1"],
    }
    with (
        patch(
            "app.services.nats_consumer._teardown_from_ledgers",
            new_callable=AsyncMock,
        ) as mock_teardown,
        patch(
            "app.services.nats_consumer._execute_dynamic_teardown",
            new_callable=AsyncMock,
        ) as mock_dyn,
        patch(
            "app.services.nats_consumer.handle_wiring_changed",
            new_callable=AsyncMock,
        ) as mock_wiring,
        # Best-effort side-effects on the terminal path (tier flip #24, wiring freeze
        # #345) are not what this dispatch test asserts; patch them out so they do not
        # run real DB code against the mock session.
        patch(
            "app.services.nats_consumer.apply_reservation_event_tiers",
            new_callable=AsyncMock,
        ),
        patch(
            "app.services.l1_assignment_service.freeze_reservation_wiring",
            new_callable=AsyncMock,
        ) as mock_freeze,
    ):
        await handle_reservation_event(event_data, _noop_get_db_session)
        mock_teardown.assert_awaited_once()
        assert str(mock_teardown.await_args.args[0]) == rid
        mock_dyn.assert_awaited_once()
        mock_freeze.assert_awaited_once()
        # A terminal event does not drive the fork reconcile.
        mock_wiring.assert_not_awaited()


# --- process_reservation_message: DLQ / retry semantics ---


def _make_msg(payload: bytes, *, num_delivered: int = 1):
    """Build a fake JetStream msg with ack/nak AsyncMocks and metadata."""
    msg = MagicMock()
    msg.data = payload
    msg.metadata = SimpleNamespace(num_delivered=num_delivered)
    msg.ack = AsyncMock()
    msg.nak = AsyncMock()
    return msg


def _make_js():
    js = MagicMock()
    js.publish = AsyncMock()
    return js


@pytest.mark.asyncio
async def test_process_message_happy_path_acks():
    js = _make_js()
    msg = _make_msg(json.dumps({"event": "reservation.created"}).encode())
    handler = AsyncMock()

    result = await process_reservation_message(msg, js, handler, session_factory=lambda: None)

    assert result == "ack"
    msg.ack.assert_awaited_once()
    msg.nak.assert_not_awaited()
    js.publish.assert_not_awaited()
    handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_process_message_poison_json_routes_to_dlq_and_acks():
    js = _make_js()
    msg = _make_msg(b"this is not json at all")
    handler = AsyncMock()

    result = await process_reservation_message(msg, js, handler, session_factory=lambda: None)

    assert result == "dlq"
    handler.assert_not_awaited()
    js.publish.assert_awaited_once_with(NATS_DLQ_SUBJECT, b"this is not json at all")
    msg.ack.assert_awaited_once()
    msg.nak.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_message_transient_error_naks_below_max_deliver():
    js = _make_js()
    msg = _make_msg(json.dumps({"event": "reservation.created"}).encode(), num_delivered=2)
    handler = AsyncMock(side_effect=RuntimeError("transient db blip"))

    result = await process_reservation_message(
        msg, js, handler, session_factory=lambda: None, max_deliver=5
    )

    assert result == "nak"
    msg.nak.assert_awaited_once()
    msg.ack.assert_not_awaited()
    js.publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_message_at_max_deliver_routes_to_dlq_and_acks():
    js = _make_js()
    payload = json.dumps({"event": "reservation.created"}).encode()
    msg = _make_msg(payload, num_delivered=NATS_MAX_DELIVER)
    handler = AsyncMock(side_effect=RuntimeError("persistent failure"))

    result = await process_reservation_message(
        msg, js, handler, session_factory=lambda: None, max_deliver=NATS_MAX_DELIVER
    )

    assert result == "dlq"
    js.publish.assert_awaited_once_with(NATS_DLQ_SUBJECT, payload)
    msg.ack.assert_awaited_once()
    msg.nak.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_message_permanent_error_dlqs_on_first_delivery(caplog):
    """A PermanentEventError (VLAN exhaustion) DLQs on the FIRST delivery (#211).

    Unlike a transient RuntimeError, which NAKs and burns the full max_deliver
    backoff before DLQ'ing, a permanent error is routed straight to the DLQ at
    delivery count 1 with a distinct exhaustion-tagged log phrase. Retrying is
    pointless: the fabric's in-use VLAN set is unchanged between attempts.
    """
    import logging

    js = _make_js()
    payload = json.dumps({"event": "reservation.created"}).encode()
    # num_delivered=1: this is the FIRST delivery, well below max_deliver.
    msg = _make_msg(payload, num_delivered=1)
    handler = AsyncMock(
        side_effect=PermanentEventError("No free VLAN IDs in fabric abc (all 4093 in use)")
    )

    with caplog.at_level(logging.ERROR):
        result = await process_reservation_message(
            msg, js, handler, session_factory=lambda: None, max_deliver=NATS_MAX_DELIVER
        )

    # Routed to DLQ immediately, ACK'd (not NAK'd), without consuming retries.
    assert result == "dlq"
    js.publish.assert_awaited_once_with(NATS_DLQ_SUBJECT, payload)
    msg.ack.assert_awaited_once()
    msg.nak.assert_not_awaited()

    # Distinct permanent-error signal, separate from the generic DLQ-exhausted
    # path. PermanentEventError now covers config errors beyond VLAN exhaustion
    # (e.g. a dynamic recipe's missing template/hypervisor/secret), so the marker
    # is the action tag plus the "permanent error" phrase.
    assert any(
        "permanent error" in rec.getMessage().lower()
        and getattr(rec, "action", None) == "nats_dlq_permanent"
        for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_process_message_dlq_publish_failure_does_not_propagate():
    """If the DLQ publish itself fails, we still ack so the loop keeps draining."""
    js = _make_js()
    js.publish.side_effect = RuntimeError("nats down")
    msg = _make_msg(b"{invalid")
    handler = AsyncMock()

    # Should not raise even though publish throws.
    result = await process_reservation_message(msg, js, handler, session_factory=lambda: None)

    assert result == "dlq"
    msg.ack.assert_awaited_once()


# --- regression (#74): DLQ subject must not match the consumer filter ---

# The consumer subscribes with this filter; see start_nats_consumer's
# js.subscribe("herd.reservations.*", ...). Kept here as a literal so the test
# fails if the subscribe filter and the DLQ subject ever realign.
CONSUMER_FILTER_SUBJECT = "herd.reservations.*"


def _nats_subject_matches(subject: str, filter_subject: str) -> bool:
    """Return whether `subject` matches a NATS `filter_subject`.

    Implements the two NATS wildcards token by token: `*` matches exactly one
    token, `>` matches one or more trailing tokens. Tokens are split on `.`.
    """
    subj_tokens = subject.split(".")
    filt_tokens = filter_subject.split(".")
    for i, ftok in enumerate(filt_tokens):
        if ftok == ">":
            # `>` is only valid as the final token and matches the rest.
            return i < len(subj_tokens)
        if i >= len(subj_tokens):
            return False
        if ftok != "*" and ftok != subj_tokens[i]:
            return False
    # No `>` consumed the tail, so token counts must match exactly.
    return len(subj_tokens) == len(filt_tokens)


def test_nats_subject_matcher_self_check():
    """Sanity-check the matcher against the single-wildcard rule the bug hinges on."""
    # `*` matches exactly one token, so the old 3-token DLQ subject DID match.
    assert _nats_subject_matches("herd.reservations.dlq", "herd.reservations.*")
    # A 4-token subject does NOT match the single-wildcard 3-token filter.
    assert not _nats_subject_matches("herd.reservations.dlq.execution", "herd.reservations.*")
    # Real lifecycle subjects still match (the consumer must keep receiving them).
    assert _nats_subject_matches("herd.reservations.created", "herd.reservations.*")


def test_dlq_subject_not_redelivered_to_consumer():
    """#74: a DLQ'd message must not be redelivered to the execution consumer.

    If NATS_DLQ_SUBJECT matched the consumer's filter, every DLQ publish would
    loop back into the same consumer: a poison message forever, and a
    max_deliver-exhausted message re-running the non-idempotent handler.
    """
    assert not _nats_subject_matches(NATS_DLQ_SUBJECT, CONSUMER_FILTER_SUBJECT)
    # And it is strictly more specific (more tokens) than the wildcard filter,
    # which is what guarantees the single-token `*` cannot reach it.
    assert len(NATS_DLQ_SUBJECT.split(".")) > len(CONSUMER_FILTER_SUBJECT.split("."))


# --- fetch helpers: transient (5xx / transport) vs genuine 404 ---


def _patch_httpx_get(*, status_code=None, raises=None, json_body=None):
    """Patch httpx.AsyncClient so `async with httpx.AsyncClient() as c: c.get(...)`
    returns a response with the given status_code (and optional JSON body), or
    raises `raises` from the .get call to simulate a transport error.
    """
    client = AsyncMock()
    if raises is not None:
        client.get.side_effect = raises
    else:
        resp = MagicMock()
        resp.status_code = status_code
        resp.json.return_value = json_body if json_body is not None else {}
        client.get.return_value = resp

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return patch("app.services.nats_consumer.httpx.AsyncClient", return_value=ctx)


@pytest.mark.asyncio
async def test_fetch_device_raises_on_5xx():
    with _patch_httpx_get(status_code=503):
        with pytest.raises(TransientUpstreamError):
            await _fetch_device("dev-1")


@pytest.mark.asyncio
async def test_fetch_device_returns_none_on_404():
    with _patch_httpx_get(status_code=404):
        assert await _fetch_device("dev-1") is None


@pytest.mark.asyncio
async def test_fetch_template_raises_on_5xx():
    with _patch_httpx_get(status_code=500):
        with pytest.raises(TransientUpstreamError):
            await _fetch_template("tpl-1")


@pytest.mark.asyncio
async def test_fetch_template_returns_none_on_404():
    with _patch_httpx_get(status_code=404):
        assert await _fetch_template("tpl-1") is None


@pytest.mark.asyncio
async def test_fetch_device_raises_on_transport_error():
    with _patch_httpx_get(raises=httpx.ConnectError("connection refused")):
        with pytest.raises(TransientUpstreamError):
            await _fetch_device("dev-1")


@pytest.mark.asyncio
async def test_fetch_context_memoizes_device_and_config_fetches():
    """_FetchContext's per-event memoization contract, re-expressed from the retired
    shared-ctx tests: a device (present or 404-None) and a latest-config are fetched at
    most once per event, and a TransientUpstreamError is NOT cached (a NAK retry should
    refetch)."""
    from app.services.nats_consumer import _FetchContext

    device = {"id": "dev-1", "connection_type": "Layer 1 Switch"}
    fetch_device = AsyncMock(side_effect=[device, None])
    fetch_config = AsyncMock(return_value={"config": {}})
    boom = AsyncMock(side_effect=TransientUpstreamError("503"))

    with (
        patch("app.services.nats_consumer._fetch_device", new=fetch_device),
        patch("app.services.nats_consumer._fetch_latest_config", new=fetch_config),
    ):
        ctx = _FetchContext(None)
        assert await ctx.get_device("dev-1") == device
        assert await ctx.get_device("dev-1") == device  # cached, no second fetch
        assert await ctx.get_device("dev-2") is None
        assert await ctx.get_device("dev-2") is None  # not-found is cached too
        assert fetch_device.await_count == 2
        await ctx.get_latest_config("dev-1")
        await ctx.get_latest_config("dev-1")
        assert fetch_config.await_count == 1

    with patch("app.services.nats_consumer._fetch_device", new=boom):
        ctx = _FetchContext(None)
        with pytest.raises(TransientUpstreamError):
            await ctx.get_device("dev-3")
        # The error was not cached as a value: a retry attempts the fetch again.
        with pytest.raises(TransientUpstreamError):
            await ctx.get_device("dev-3")
        assert boom.await_count == 2


# --- dedupe key switch: payload event_id over stream:sequence (issue #21) ---
#
# process_reservation_message now resolves the handler's idempotency key via
# herd_common.outbox.event_dedupe_key, which prefers the producer-stamped
# payload event_id. The stable id survives an outbox relay republish under a
# new JetStream sequence, so a duplicate delivery still dedupes to one effect;
# pre-outbox events with no event_id fall back to "<stream>:<sequence>".


def _make_msg_with_seq(payload: bytes, *, stream: str = "HERD_RESERVATIONS", seq: int = 1):
    """Fake JetStream msg carrying full metadata (stream + stream sequence)."""
    msg = MagicMock()
    msg.data = payload
    msg.metadata = SimpleNamespace(
        num_delivered=1,
        stream=stream,
        sequence=SimpleNamespace(stream=seq),
    )
    msg.ack = AsyncMock()
    msg.nak = AsyncMock()
    return msg


@pytest.mark.asyncio
async def test_process_message_keys_on_payload_event_id():
    """When the payload carries event_id, the handler is keyed on it, not the seq."""
    js = _make_js()
    eid = str(uuid.uuid4())
    payload = json.dumps({"event": "reservation.created", "event_id": eid}).encode()
    msg = _make_msg_with_seq(payload, seq=42)
    handler = AsyncMock()

    result = await process_reservation_message(msg, js, handler, session_factory=lambda: None)

    assert result == "ack"
    # handler(event_data, session_factory, dedupe_key)
    _args, _kwargs = handler.call_args
    assert _args[2] == eid
    assert _args[2] != "HERD_RESERVATIONS:42"


@pytest.mark.asyncio
async def test_process_message_same_event_id_different_sequence_dedupes():
    """Two deliveries with the same event_id but different stream sequence resolve
    to the same key, so a relay republish dedupes to one effect."""
    js = _make_js()
    eid = str(uuid.uuid4())
    payload = json.dumps({"event": "reservation.created", "event_id": eid}).encode()

    keys = []
    handler = AsyncMock(side_effect=lambda *a, **k: keys.append(a[2]))

    await process_reservation_message(
        _make_msg_with_seq(payload, seq=7), js, handler, session_factory=lambda: None
    )
    await process_reservation_message(
        _make_msg_with_seq(payload, seq=99), js, handler, session_factory=lambda: None
    )

    assert keys == [eid, eid]


@pytest.mark.asyncio
async def test_process_message_falls_back_to_stream_sequence_without_event_id():
    """A pre-outbox event with no event_id keys on '<stream>:<sequence>'."""
    js = _make_js()
    payload = json.dumps({"event": "reservation.created"}).encode()
    msg = _make_msg_with_seq(payload, stream="HERD_RESERVATIONS", seq=55)
    handler = AsyncMock()

    result = await process_reservation_message(msg, js, handler, session_factory=lambda: None)

    assert result == "ack"
    _args, _kwargs = handler.call_args
    assert _args[2] == "HERD_RESERVATIONS:55"
