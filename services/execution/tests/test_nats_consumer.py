"""Tests for nats_consumer.py: event handling and L1/L2 switch operation resolution."""

import json
import uuid
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import httpx
import pytest
from app.services.nats_consumer import (
    EVENT_ACTIONS,
    L2_EVENT_ACTIONS,
    NATS_DLQ_SUBJECT,
    NATS_MAX_DELIVER,
    PermanentEventError,
    TransientUpstreamError,
    _derive_vlan_id,
    _fetch_connections_for_device,
    _fetch_device,
    _fetch_template,
    _FetchContext,
    _resolve_l1_switch_operations,
    _resolve_l2_switch_operations,
    handle_reservation_event,
    process_reservation_message,
)

# --- EVENT_ACTIONS mapping ---


def test_event_actions_mapping():
    assert EVENT_ACTIONS["reservation.created"] == "connect_ports"
    assert EVENT_ACTIONS["reservation.cancelled"] == "disconnect_ports"
    assert EVENT_ACTIONS["reservation.completed"] == "disconnect_ports"
    assert EVENT_ACTIONS["reservation.updated"] == "update_ports"


# --- _resolve_l1_switch_operations ---


@pytest.mark.asyncio
async def test_resolve_no_connections():
    """No connections means no operations."""
    with patch(
        "app.services.nats_consumer._fetch_connections_for_device",
        new_callable=AsyncMock,
        return_value=[],
    ):
        ops = await _resolve_l1_switch_operations(["device-1"])
    assert ops == []


@pytest.mark.asyncio
async def test_resolve_connection_to_non_switch():
    """Connections to non-L1-switch devices produce no operations."""
    connection = {
        "device_a_id": "device-1",
        "port_a": "eth0",
        "device_b_id": "other-device",
        "port_b": "eth1",
    }
    other_device = {
        "id": "other-device",
        "connection_type": "Management",
    }
    with (
        patch(
            "app.services.nats_consumer._fetch_connections_for_device",
            new_callable=AsyncMock,
            return_value=[connection],
        ),
        patch(
            "app.services.nats_consumer._fetch_device",
            new_callable=AsyncMock,
            return_value=other_device,
        ),
    ):
        ops = await _resolve_l1_switch_operations(["device-1"])
    assert ops == []


@pytest.mark.asyncio
async def test_resolve_connection_to_l1_switch():
    """Two DUTs connected through an L1 switch produce a port pair operation."""
    switch_id = "switch-1"
    conn1 = {
        "device_a_id": "dut-1",
        "port_a": "eth0",
        "device_b_id": switch_id,
        "port_b": "port-A1",
    }
    conn2 = {
        "device_a_id": "dut-2",
        "port_a": "eth0",
        "device_b_id": switch_id,
        "port_b": "port-A2",
    }
    switch_device = {
        "id": switch_id,
        "connection_type": "Layer 1 Switch",
    }

    async def mock_fetch_connections(device_id, client=None):
        if device_id == "dut-1":
            return [conn1]
        if device_id == "dut-2":
            return [conn2]
        return []

    with (
        patch(
            "app.services.nats_consumer._fetch_connections_for_device",
            side_effect=mock_fetch_connections,
        ),
        patch(
            "app.services.nats_consumer._fetch_device",
            new_callable=AsyncMock,
            return_value=switch_device,
        ),
    ):
        ops = await _resolve_l1_switch_operations(["dut-1", "dut-2"])

    assert len(ops) == 1
    assert ops[0]["switch_device_id"] == switch_id
    assert ops[0]["switch_port_a"] == "port-A1"
    assert ops[0]["switch_port_b"] == "port-A2"


@pytest.mark.asyncio
async def test_resolve_device_not_found():
    """If the other device is not found, skip the connection."""
    connection = {
        "device_a_id": "device-1",
        "port_a": "eth0",
        "device_b_id": "missing-device",
        "port_b": "eth1",
    }
    with (
        patch(
            "app.services.nats_consumer._fetch_connections_for_device",
            new_callable=AsyncMock,
            return_value=[connection],
        ),
        patch(
            "app.services.nats_consumer._fetch_device",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        ops = await _resolve_l1_switch_operations(["device-1"])
    assert ops == []


@pytest.mark.asyncio
async def test_resolve_switch_is_device_a():
    """Switch can be on either side of the connection."""
    switch_id = "switch-1"
    conn1 = {
        "device_a_id": switch_id,
        "port_a": "port-X",
        "device_b_id": "dut-1",
        "port_b": "eth0",
    }
    conn2 = {
        "device_a_id": switch_id,
        "port_a": "port-Y",
        "device_b_id": "dut-2",
        "port_b": "eth0",
    }
    switch_device = {"id": switch_id, "connection_type": "Layer 1 Switch"}

    async def mock_connections(device_id, client=None):
        if device_id == "dut-1":
            return [conn1]
        if device_id == "dut-2":
            return [conn2]
        return []

    with (
        patch(
            "app.services.nats_consumer._fetch_connections_for_device",
            side_effect=mock_connections,
        ),
        patch(
            "app.services.nats_consumer._fetch_device",
            new_callable=AsyncMock,
            return_value=switch_device,
        ),
    ):
        ops = await _resolve_l1_switch_operations(["dut-1", "dut-2"])

    assert len(ops) == 1
    assert ops[0]["switch_port_a"] == "port-X"
    assert ops[0]["switch_port_b"] == "port-Y"


# --- handle_reservation_event ---


@pytest.mark.asyncio
async def test_handle_unknown_event():
    """Unknown event types are ignored."""
    event_data = {"event": "reservation.unknown", "device_ids": []}
    await handle_reservation_event(event_data, AsyncMock())
    # Should return without error


@pytest.mark.asyncio
async def test_handle_event_no_operations():
    """Event with no L1 switch operations is a no-op."""
    event_data = {
        "event": "reservation.created",
        "reservation_id": str(uuid.uuid4()),
        "user_id": str(uuid.uuid4()),
        "device_ids": ["device-1"],
    }
    with (
        patch(
            "app.services.nats_consumer._resolve_l1_switch_operations",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "app.services.nats_consumer._execute_l2_switch_operations",
            new_callable=AsyncMock,
        ),
    ):
        await handle_reservation_event(event_data, AsyncMock())


@pytest.mark.asyncio
async def test_handle_event_switch_not_found():
    """If the switch device is not found, skip it and continue."""
    event_data = {
        "event": "reservation.created",
        "reservation_id": str(uuid.uuid4()),
        "user_id": str(uuid.uuid4()),
        "device_ids": ["dut-1", "dut-2"],
    }
    ops = [{"switch_device_id": "missing-switch", "switch_port_a": "1", "switch_port_b": "2"}]

    with (
        patch(
            "app.services.nats_consumer._resolve_l1_switch_operations",
            new_callable=AsyncMock,
            return_value=ops,
        ),
        patch(
            "app.services.nats_consumer._fetch_device",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "app.services.nats_consumer._execute_l2_switch_operations",
            new_callable=AsyncMock,
        ),
    ):
        await handle_reservation_event(event_data, AsyncMock())


@pytest.mark.asyncio
async def test_handle_event_template_not_found():
    """If the switch template is not found, skip the switch."""
    event_data = {
        "event": "reservation.created",
        "reservation_id": str(uuid.uuid4()),
        "user_id": str(uuid.uuid4()),
        "device_ids": ["dut-1"],
    }
    ops = [{"switch_device_id": "switch-1", "switch_port_a": "1", "switch_port_b": "2"}]
    switch_data = {
        "id": "switch-1",
        "template_id": "tmpl-1",
        "driver_id": str(uuid.uuid4()),
        "driver_sha256": "abc",
        "driver_filename": "driver.zip",
        "connection_type": "Layer 1 Switch",
        "name": "Switch",
        "field_data": {},
    }

    with (
        patch(
            "app.services.nats_consumer._resolve_l1_switch_operations",
            new_callable=AsyncMock,
            return_value=ops,
        ),
        patch(
            "app.services.nats_consumer._fetch_device",
            new_callable=AsyncMock,
            return_value=switch_data,
        ),
        patch(
            "app.services.nats_consumer._fetch_template",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "app.services.nats_consumer._execute_l2_switch_operations",
            new_callable=AsyncMock,
        ),
    ):
        await handle_reservation_event(event_data, AsyncMock())


@pytest.mark.asyncio
async def test_resolve_odd_port_count():
    """Odd number of ports for a switch logs warning and skips last port."""
    switch_id = "switch-1"
    # Three DUTs connected to same switch: 3 ports, only 1 pair formed
    conn1 = {
        "device_a_id": "dut-1",
        "port_a": "eth0",
        "device_b_id": switch_id,
        "port_b": "port-A1",
    }
    conn2 = {
        "device_a_id": "dut-2",
        "port_a": "eth0",
        "device_b_id": switch_id,
        "port_b": "port-A2",
    }
    conn3 = {
        "device_a_id": "dut-3",
        "port_a": "eth0",
        "device_b_id": switch_id,
        "port_b": "port-A3",
    }
    switch_device = {"id": switch_id, "connection_type": "Layer 1 Switch"}

    async def mock_connections(device_id, client=None):
        mapping = {"dut-1": [conn1], "dut-2": [conn2], "dut-3": [conn3]}
        return mapping.get(device_id, [])

    with (
        patch(
            "app.services.nats_consumer._fetch_connections_for_device",
            side_effect=mock_connections,
        ),
        patch(
            "app.services.nats_consumer._fetch_device",
            new_callable=AsyncMock,
            return_value=switch_device,
        ),
    ):
        ops = await _resolve_l1_switch_operations(["dut-1", "dut-2", "dut-3"])

    # 3 ports, only 1 pair (port-A1, port-A2); port-A3 is unpaired
    assert len(ops) == 1
    assert ops[0]["switch_port_a"] == "port-A1"
    assert ops[0]["switch_port_b"] == "port-A2"


@pytest.mark.asyncio
async def test_resolve_connection_neither_side_matches():
    """Connection where neither side matches the device_id is skipped (line 90)."""
    # This is an edge case: _fetch_connections_for_device returns a connection
    # where neither device_a_id nor device_b_id equals the queried device_id
    connection = {
        "device_a_id": "other-1",
        "port_a": "eth0",
        "device_b_id": "other-2",
        "port_b": "eth1",
    }
    with patch(
        "app.services.nats_consumer._fetch_connections_for_device",
        new_callable=AsyncMock,
        return_value=[connection],
    ):
        ops = await _resolve_l1_switch_operations(["device-1"])
    assert ops == []


# --- reservation.updated event handling ---


@pytest.mark.asyncio
async def test_handle_updated_event_no_changes():
    """Updated event with empty added/removed lists is a no-op."""
    event_data = {
        "event": "reservation.updated",
        "reservation_id": str(uuid.uuid4()),
        "user_id": str(uuid.uuid4()),
        "device_ids": ["device-1"],
        "added_device_ids": [],
        "removed_device_ids": [],
    }
    with (
        patch(
            "app.services.nats_consumer._execute_switch_operations",
            new_callable=AsyncMock,
        ) as mock_exec,
        patch(
            "app.services.nats_consumer._execute_l2_switch_operations",
            new_callable=AsyncMock,
        ) as mock_l2_exec,
    ):
        await handle_reservation_event(event_data, AsyncMock())
        mock_exec.assert_not_called()
        mock_l2_exec.assert_not_called()


@pytest.mark.asyncio
async def test_handle_updated_event_connect_added_devices():
    """Updated event with added_device_ids triggers connect_ports and L2 provision."""
    rid = str(uuid.uuid4())
    uid = str(uuid.uuid4())
    event_data = {
        "event": "reservation.updated",
        "reservation_id": rid,
        "user_id": uid,
        "device_ids": ["device-1", "device-2"],
        "added_device_ids": ["device-2"],
        "removed_device_ids": [],
    }
    mock_session = AsyncMock()
    with (
        patch(
            "app.services.nats_consumer._execute_switch_operations",
            new_callable=AsyncMock,
        ) as mock_exec,
        patch(
            "app.services.nats_consumer._execute_l2_switch_operations",
            new_callable=AsyncMock,
        ) as mock_l2_exec,
    ):
        await handle_reservation_event(event_data, mock_session)
        # ANY is the per-event _FetchContext threaded through (issue #137).
        mock_exec.assert_called_once_with(
            ["device-2"], "connect_ports", rid, uid, mock_session, None, ANY
        )
        mock_l2_exec.assert_called_once_with(
            ["device-2"], "provision", rid, uid, mock_session, None, ANY
        )


@pytest.mark.asyncio
async def test_handle_updated_event_disconnect_removed_devices():
    """Updated event with removed_device_ids triggers disconnect_ports and L2 deprovision."""
    rid = str(uuid.uuid4())
    uid = str(uuid.uuid4())
    event_data = {
        "event": "reservation.updated",
        "reservation_id": rid,
        "user_id": uid,
        "device_ids": ["device-1"],
        "added_device_ids": [],
        "removed_device_ids": ["device-3"],
    }
    mock_session = AsyncMock()
    with (
        patch(
            "app.services.nats_consumer._execute_switch_operations",
            new_callable=AsyncMock,
        ) as mock_exec,
        patch(
            "app.services.nats_consumer._execute_l2_switch_operations",
            new_callable=AsyncMock,
        ) as mock_l2_exec,
    ):
        await handle_reservation_event(event_data, mock_session)
        mock_exec.assert_called_once_with(
            ["device-3"], "disconnect_ports", rid, uid, mock_session, None, ANY
        )
        mock_l2_exec.assert_called_once_with(
            ["device-3"], "deprovision", rid, uid, mock_session, None, ANY
        )


@pytest.mark.asyncio
async def test_handle_updated_event_both_added_and_removed():
    """Updated event with both added and removed devices triggers both L1 and L2 operations."""
    rid = str(uuid.uuid4())
    uid = str(uuid.uuid4())
    event_data = {
        "event": "reservation.updated",
        "reservation_id": rid,
        "user_id": uid,
        "device_ids": ["device-1", "device-2"],
        "added_device_ids": ["device-2"],
        "removed_device_ids": ["device-3"],
    }
    mock_session = AsyncMock()
    with (
        patch(
            "app.services.nats_consumer._execute_switch_operations",
            new_callable=AsyncMock,
        ) as mock_exec,
        patch(
            "app.services.nats_consumer._execute_l2_switch_operations",
            new_callable=AsyncMock,
        ) as mock_l2_exec,
    ):
        await handle_reservation_event(event_data, mock_session)
        assert mock_exec.call_count == 2
        mock_exec.assert_any_call(["device-2"], "connect_ports", rid, uid, mock_session, None, ANY)
        mock_exec.assert_any_call(
            ["device-3"], "disconnect_ports", rid, uid, mock_session, None, ANY
        )
        assert mock_l2_exec.call_count == 2
        mock_l2_exec.assert_any_call(["device-2"], "provision", rid, uid, mock_session, None, ANY)
        mock_l2_exec.assert_any_call(["device-3"], "deprovision", rid, uid, mock_session, None, ANY)


# --- L2 Switch Operation Resolution ---


def test_derive_vlan_id_range():
    """VLAN ID derived from reservation UUID is in range 2-4094."""
    for _ in range(100):
        rid = str(uuid.uuid4())
        vlan = _derive_vlan_id(rid)
        assert 2 <= vlan <= 4094


def test_derive_vlan_id_deterministic():
    """Same reservation_id always produces the same VLAN ID."""
    rid = str(uuid.uuid4())
    assert _derive_vlan_id(rid) == _derive_vlan_id(rid)


def test_l2_event_actions_mapping():
    """L2 event actions map correctly."""
    assert L2_EVENT_ACTIONS["reservation.created"] == "provision"
    assert L2_EVENT_ACTIONS["reservation.cancelled"] == "deprovision"
    assert L2_EVENT_ACTIONS["reservation.completed"] == "deprovision"
    assert "reservation.updated" not in L2_EVENT_ACTIONS


@pytest.mark.asyncio
async def test_resolve_l2_no_connections():
    """No connections means no L2 operations."""
    with patch(
        "app.services.nats_consumer._fetch_connections_for_device",
        new_callable=AsyncMock,
        return_value=[],
    ):
        ops = await _resolve_l2_switch_operations(["device-1"])
    assert ops == []


@pytest.mark.asyncio
async def test_resolve_l2_connection_to_l2_switch():
    """DUT connected to an L2 switch produces a per-port operation."""
    switch_id = "l2-switch-1"
    conn = {
        "device_a_id": "dut-1",
        "port_a": "eth6",
        "device_b_id": switch_id,
        "port_b": "eth3",
    }
    switch_device = {"id": switch_id, "connection_type": "Layer 2 Switch"}

    with (
        patch(
            "app.services.nats_consumer._fetch_connections_for_device",
            new_callable=AsyncMock,
            return_value=[conn],
        ),
        patch(
            "app.services.nats_consumer._fetch_device",
            new_callable=AsyncMock,
            return_value=switch_device,
        ),
    ):
        ops = await _resolve_l2_switch_operations(["dut-1"])

    assert len(ops) == 1
    assert ops[0]["switch_device_id"] == switch_id
    assert ops[0]["switch_port"] == "eth3"
    assert "vlan_id" not in ops[0]  # VLAN assigned later by fabric-aware service
    assert ops[0]["tag"] == "tagged"


@pytest.mark.asyncio
async def test_resolve_l2_ignores_l1_switches():
    """L1 switches are not included in L2 resolution."""
    conn = {
        "device_a_id": "dut-1",
        "port_a": "eth1",
        "device_b_id": "l1-switch",
        "port_b": "0/0/1",
    }
    l1_device = {"id": "l1-switch", "connection_type": "Layer 1 Switch"}

    with (
        patch(
            "app.services.nats_consumer._fetch_connections_for_device",
            new_callable=AsyncMock,
            return_value=[conn],
        ),
        patch(
            "app.services.nats_consumer._fetch_device",
            new_callable=AsyncMock,
            return_value=l1_device,
        ),
    ):
        ops = await _resolve_l2_switch_operations(["dut-1"])

    assert ops == []


@pytest.mark.asyncio
async def test_resolve_l2_multiple_ports_not_paired():
    """L2 operations are per-port, not paired like L1."""
    switch_id = "l2-switch-1"
    conn1 = {
        "device_a_id": "dut-1",
        "port_a": "eth6",
        "device_b_id": switch_id,
        "port_b": "eth1",
    }
    conn2 = {
        "device_a_id": "dut-2",
        "port_a": "eth6",
        "device_b_id": switch_id,
        "port_b": "eth2",
    }
    conn3 = {
        "device_a_id": "dut-3",
        "port_a": "eth6",
        "device_b_id": switch_id,
        "port_b": "eth3",
    }
    switch_device = {"id": switch_id, "connection_type": "Layer 2 Switch"}

    async def mock_connections(device_id, client=None):
        mapping = {"dut-1": [conn1], "dut-2": [conn2], "dut-3": [conn3]}
        return mapping.get(device_id, [])

    with (
        patch(
            "app.services.nats_consumer._fetch_connections_for_device",
            side_effect=mock_connections,
        ),
        patch(
            "app.services.nats_consumer._fetch_device",
            new_callable=AsyncMock,
            return_value=switch_device,
        ),
    ):
        ops = await _resolve_l2_switch_operations(["dut-1", "dut-2", "dut-3"])

    # 3 per-port operations (not 1 pair like L1 would produce)
    assert len(ops) == 3
    switch_ports = {op["switch_port"] for op in ops}
    assert switch_ports == {"eth1", "eth2", "eth3"}


@pytest.mark.asyncio
async def test_handle_created_event_calls_l2_provision():
    """reservation.created triggers both L1 connect and L2 provision."""
    rid = str(uuid.uuid4())
    uid = str(uuid.uuid4())
    event_data = {
        "event": "reservation.created",
        "reservation_id": rid,
        "user_id": uid,
        "device_ids": ["device-1"],
    }
    mock_session = AsyncMock()
    with (
        patch(
            "app.services.nats_consumer._execute_switch_operations",
            new_callable=AsyncMock,
        ) as mock_l1,
        patch(
            "app.services.nats_consumer._execute_l2_switch_operations",
            new_callable=AsyncMock,
        ) as mock_l2,
    ):
        await handle_reservation_event(event_data, mock_session)
        mock_l1.assert_called_once_with(
            ["device-1"], "connect_ports", rid, uid, mock_session, None, ANY
        )
        mock_l2.assert_called_once_with(
            ["device-1"], "provision", rid, uid, mock_session, None, ANY
        )


@pytest.mark.asyncio
async def test_handle_cancelled_event_calls_l2_deprovision():
    """reservation.cancelled triggers both L1 disconnect and L2 deprovision."""
    rid = str(uuid.uuid4())
    uid = str(uuid.uuid4())
    event_data = {
        "event": "reservation.cancelled",
        "reservation_id": rid,
        "user_id": uid,
        "device_ids": ["device-1"],
    }
    mock_session = AsyncMock()
    with (
        patch(
            "app.services.nats_consumer._execute_switch_operations",
            new_callable=AsyncMock,
        ) as mock_l1,
        patch(
            "app.services.nats_consumer._execute_l2_switch_operations",
            new_callable=AsyncMock,
        ) as mock_l2,
    ):
        await handle_reservation_event(event_data, mock_session)
        mock_l1.assert_called_once_with(
            ["device-1"], "disconnect_ports", rid, uid, mock_session, None, ANY
        )
        mock_l2.assert_called_once_with(
            ["device-1"], "deprovision", rid, uid, mock_session, None, ANY
        )


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

    # Distinct exhaustion-tagged signal, separate from the generic DLQ-exhausted path.
    assert any(
        "exhaustion" in rec.getMessage().lower()
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
async def test_fetch_connections_raises_on_5xx():
    with _patch_httpx_get(status_code=502):
        with pytest.raises(TransientUpstreamError):
            await _fetch_connections_for_device("dev-1")


@pytest.mark.asyncio
async def test_fetch_connections_returns_empty_on_non_5xx_error():
    # A 404 (or any sub-500 non-200) is treated as "no connections", not transient.
    with _patch_httpx_get(status_code=404):
        assert await _fetch_connections_for_device("dev-1") == []


@pytest.mark.asyncio
async def test_fetch_device_raises_on_transport_error():
    with _patch_httpx_get(raises=httpx.ConnectError("connection refused")):
        with pytest.raises(TransientUpstreamError):
            await _fetch_device("dev-1")


# --- end-to-end: a 5xx during provisioning NAKs, then DLQs at max_deliver ---


def _reservation_created_payload():
    return json.dumps(
        {
            "event": "reservation.created",
            "reservation_id": str(uuid.uuid4()),
            "user_id": str(uuid.uuid4()),
            "device_ids": [str(uuid.uuid4())],
        }
    ).encode()


@pytest.mark.asyncio
async def test_upstream_5xx_naks_below_max_deliver():
    """A 5xx from cabling while resolving operations must propagate so the
    message NAKs for retry, not ACK as a silent no-op."""
    js = _make_js()
    msg = _make_msg(_reservation_created_payload(), num_delivered=1)

    with _patch_httpx_get(status_code=503):
        result = await process_reservation_message(
            msg, js, handle_reservation_event, session_factory=lambda: None
        )

    assert result == "nak"
    msg.nak.assert_awaited_once()
    msg.ack.assert_not_awaited()
    js.publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_upstream_5xx_routes_to_dlq_at_max_deliver():
    """Once redelivery is exhausted, the same persistent 5xx routes to the DLQ
    and acks, rather than looping forever."""
    js = _make_js()
    payload = _reservation_created_payload()
    msg = _make_msg(payload, num_delivered=NATS_MAX_DELIVER)

    with _patch_httpx_get(status_code=503):
        result = await process_reservation_message(
            msg,
            js,
            handle_reservation_event,
            session_factory=lambda: None,
            max_deliver=NATS_MAX_DELIVER,
        )

    assert result == "dlq"
    js.publish.assert_awaited_once_with(NATS_DLQ_SUBJECT, payload)
    msg.ack.assert_awaited_once()
    msg.nak.assert_not_awaited()


# --- issue #137: per-event shared client + memoization caches ---
#
# The resolvers used to open a fresh httpx.AsyncClient per request and re-fetch
# each device's connections (once per resolver pass) and each far-end device
# (once per connection touching it). These tests pin the new behavior: a single
# _FetchContext threaded through both passes fetches each device's connections
# at most once and classifies each far-end device at most once, while producing
# the same operations and preserving the TransientUpstreamError retry semantics.


def _counting_fetch_patches(connections_by_device, devices_by_id):
    """Patch the two module-level fetch helpers with call-counting AsyncMocks.

    The cache-aware _FetchContext calls these module names, so patching them and
    inspecting call_count proves how many real round-trips a context would make.
    Both stubs accept the optional `client` arg the cache wrapper passes.
    """

    async def fetch_conns(device_id, client=None):
        return connections_by_device.get(device_id, [])

    async def fetch_dev(device_id, client=None):
        return devices_by_id.get(device_id)

    conns_mock = AsyncMock(side_effect=fetch_conns)
    dev_mock = AsyncMock(side_effect=fetch_dev)
    return (
        conns_mock,
        dev_mock,
        patch("app.services.nats_consumer._fetch_connections_for_device", conns_mock),
        patch("app.services.nats_consumer._fetch_device", dev_mock),
    )


@pytest.mark.asyncio
async def test_shared_far_end_switch_fetched_once_per_event():
    """A far-end switch shared by multiple reserved devices is fetched exactly
    once across the whole event, not once per connection touching it."""
    switch_id = "shared-switch"
    # Three DUTs all cabled to the same L1 switch on distinct switch ports.
    connections_by_device = {
        "dut-1": [
            {"device_a_id": "dut-1", "port_a": "eth0", "device_b_id": switch_id, "port_b": "p1"}
        ],
        "dut-2": [
            {"device_a_id": "dut-2", "port_a": "eth0", "device_b_id": switch_id, "port_b": "p2"}
        ],
        "dut-3": [
            {"device_a_id": "dut-3", "port_a": "eth0", "device_b_id": switch_id, "port_b": "p3"}
        ],
    }
    devices_by_id = {switch_id: {"id": switch_id, "connection_type": "Layer 1 Switch"}}

    conns_mock, dev_mock, p_conns, p_dev = _counting_fetch_patches(
        connections_by_device, devices_by_id
    )
    with p_conns, p_dev:
        ctx = _FetchContext(object())  # a non-None sentinel client; never used by the stubs
        await _resolve_l1_switch_operations(["dut-1", "dut-2", "dut-3"], ctx)

    # The shared switch was classified once, even though 3 connections reference it.
    switch_fetches = [c for c in dev_mock.call_args_list if c.args[0] == switch_id]
    assert len(switch_fetches) == 1, f"switch fetched {len(switch_fetches)} times, expected 1"
    # Each DUT's connection set was fetched exactly once.
    assert conns_mock.call_count == 3


@pytest.mark.asyncio
async def test_connections_not_refetched_across_l1_and_l2_passes():
    """reservation.created runs BOTH the L1 and L2 resolvers; with a shared ctx
    each device's connections are fetched once total, not once per pass."""
    switch_l1 = "l1-switch"
    switch_l2 = "l2-switch"
    connections_by_device = {
        "dut-1": [
            {"device_a_id": "dut-1", "port_a": "eth0", "device_b_id": switch_l1, "port_b": "a1"},
            {"device_a_id": "dut-1", "port_a": "eth1", "device_b_id": switch_l2, "port_b": "b1"},
        ],
        "dut-2": [
            {"device_a_id": "dut-2", "port_a": "eth0", "device_b_id": switch_l1, "port_b": "a2"},
        ],
    }
    devices_by_id = {
        switch_l1: {"id": switch_l1, "connection_type": "Layer 1 Switch"},
        switch_l2: {"id": switch_l2, "connection_type": "Layer 2 Switch"},
    }

    conns_mock, dev_mock, p_conns, p_dev = _counting_fetch_patches(
        connections_by_device, devices_by_id
    )
    device_ids = ["dut-1", "dut-2"]
    with p_conns, p_dev:
        ctx = _FetchContext(object())
        await _resolve_l1_switch_operations(device_ids, ctx)
        await _resolve_l2_switch_operations(device_ids, ctx)

    # Two reserved devices, two passes: still only one connection fetch per device.
    assert conns_mock.call_count == 2, (
        f"connections fetched {conns_mock.call_count} times, expected 2 "
        "(once per device, shared across L1+L2)"
    )
    # Each far-end switch classified once despite being seen in both passes.
    l1_fetches = [c for c in dev_mock.call_args_list if c.args[0] == switch_l1]
    l2_fetches = [c for c in dev_mock.call_args_list if c.args[0] == switch_l2]
    assert len(l1_fetches) == 1
    assert len(l2_fetches) == 1


@pytest.mark.asyncio
async def test_shared_ctx_produces_same_ops_as_independent_resolution():
    """The shared-context resolution must yield byte-identical L1/L2 operations
    to resolving each pass with its own (legacy-style) context. Only call
    efficiency changes, never the result."""
    switch_l1 = "l1-switch"
    switch_l2 = "l2-switch"
    connections_by_device = {
        "dut-1": [
            {"device_a_id": "dut-1", "port_a": "eth0", "device_b_id": switch_l1, "port_b": "a1"},
            {"device_a_id": "dut-1", "port_a": "eth1", "device_b_id": switch_l2, "port_b": "b1"},
        ],
        "dut-2": [
            {"device_a_id": switch_l1, "port_a": "a2", "device_b_id": "dut-2", "port_b": "eth0"},
            {"device_a_id": "dut-2", "port_a": "eth1", "device_b_id": switch_l2, "port_b": "b2"},
        ],
        "dut-3": [
            # A connection to a plain management device: produces no switch op.
            {"device_a_id": "dut-3", "port_a": "eth0", "device_b_id": "mgmt", "port_b": "e0"},
        ],
    }
    devices_by_id = {
        switch_l1: {"id": switch_l1, "connection_type": "Layer 1 Switch"},
        switch_l2: {"id": switch_l2, "connection_type": "Layer 2 Switch"},
        "mgmt": {"id": "mgmt", "connection_type": "Management"},
    }
    device_ids = ["dut-1", "dut-2", "dut-3"]

    _, _, p_conns, p_dev = _counting_fetch_patches(connections_by_device, devices_by_id)

    with p_conns, p_dev:
        # Independent, per-pass contexts (mirrors the pre-#137 per-call behavior).
        l1_independent = await _resolve_l1_switch_operations(device_ids, _FetchContext(object()))
        l2_independent = await _resolve_l2_switch_operations(device_ids, _FetchContext(object()))

        # One shared context across both passes (the #137 behavior).
        shared = _FetchContext(object())
        l1_shared = await _resolve_l1_switch_operations(device_ids, shared)
        l2_shared = await _resolve_l2_switch_operations(device_ids, shared)

    assert l1_shared == l1_independent
    assert l2_shared == l2_independent
    # Sanity: the topology actually exercises both an L1 pair and an L2 per-port op.
    assert l1_shared and l1_shared[0]["switch_device_id"] == switch_l1
    assert {op["switch_device_id"] for op in l2_shared} == {switch_l2}


@pytest.mark.asyncio
async def test_shared_ctx_propagates_transient_upstream_error():
    """A 5xx (or transport error) surfaced through the shared client still raises
    TransientUpstreamError so the message NAKs and JetStream retries (issue #137
    must not weaken the #131/#133 retry semantics)."""
    # Drive a real _FetchContext with a real (mocked) client whose .get returns 503.
    with _patch_httpx_get(status_code=503):
        async with httpx.AsyncClient() as client:
            ctx = _FetchContext(client)
            with pytest.raises(TransientUpstreamError):
                await _resolve_l1_switch_operations(["dut-1"], ctx)


@pytest.mark.asyncio
async def test_shared_ctx_caches_not_found_far_end_once():
    """A far-end device that 404s (returns None) is cached as a real miss, so a
    second connection to the same missing id does not re-fetch it."""
    connections_by_device = {
        "dut-1": [
            {"device_a_id": "dut-1", "port_a": "eth0", "device_b_id": "ghost", "port_b": "x"},
        ],
        "dut-2": [
            {"device_a_id": "dut-2", "port_a": "eth0", "device_b_id": "ghost", "port_b": "y"},
        ],
    }
    devices_by_id = {}  # "ghost" is absent: fetch returns None

    conns_mock, dev_mock, p_conns, p_dev = _counting_fetch_patches(
        connections_by_device, devices_by_id
    )
    with p_conns, p_dev:
        ctx = _FetchContext(object())
        ops = await _resolve_l1_switch_operations(["dut-1", "dut-2"], ctx)

    assert ops == []
    ghost_fetches = [c for c in dev_mock.call_args_list if c.args[0] == "ghost"]
    assert len(ghost_fetches) == 1, "a missing far end must be classified at most once"


@pytest.mark.asyncio
async def test_handle_event_opens_single_client_for_whole_event():
    """handle_reservation_event opens exactly one httpx.AsyncClient per event and
    threads it through both resolver passes, replacing the prior per-request
    client churn (issue #137)."""
    rid = str(uuid.uuid4())
    uid = str(uuid.uuid4())
    event_data = {
        "event": "reservation.created",
        "reservation_id": rid,
        "user_id": uid,
        "device_ids": ["dut-1"],
    }

    real_async_client = httpx.AsyncClient
    created = []

    def _track_client(*args, **kwargs):
        client = real_async_client(*args, **kwargs)
        created.append(client)
        return client

    with (
        patch("app.services.nats_consumer.httpx.AsyncClient", side_effect=_track_client),
        # Keep the execution side-effects out; we only care about client lifecycle.
        patch(
            "app.services.nats_consumer._execute_switch_operations",
            new_callable=AsyncMock,
        ),
        patch(
            "app.services.nats_consumer._execute_l2_switch_operations",
            new_callable=AsyncMock,
        ),
    ):
        await handle_reservation_event(event_data, AsyncMock())

    assert len(created) == 1, f"expected exactly one AsyncClient per event, got {len(created)}"


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
