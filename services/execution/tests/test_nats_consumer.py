"""Tests for nats_consumer.py: event handling and L1/L2 switch operation resolution."""

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from app.services.nats_consumer import (
    EVENT_ACTIONS,
    L2_EVENT_ACTIONS,
    NATS_DLQ_SUBJECT,
    NATS_MAX_DELIVER,
    TransientUpstreamError,
    _derive_vlan_id,
    _fetch_connections_for_device,
    _fetch_device,
    _fetch_template,
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

    async def mock_fetch_connections(device_id):
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

    async def mock_connections(device_id):
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

    async def mock_connections(device_id):
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
        mock_exec.assert_called_once_with(["device-2"], "connect_ports", rid, uid, mock_session)
        mock_l2_exec.assert_called_once_with(["device-2"], "provision", rid, uid, mock_session)


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
        mock_exec.assert_called_once_with(["device-3"], "disconnect_ports", rid, uid, mock_session)
        mock_l2_exec.assert_called_once_with(["device-3"], "deprovision", rid, uid, mock_session)


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
        mock_exec.assert_any_call(["device-2"], "connect_ports", rid, uid, mock_session)
        mock_exec.assert_any_call(["device-3"], "disconnect_ports", rid, uid, mock_session)
        assert mock_l2_exec.call_count == 2
        mock_l2_exec.assert_any_call(["device-2"], "provision", rid, uid, mock_session)
        mock_l2_exec.assert_any_call(["device-3"], "deprovision", rid, uid, mock_session)


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

    async def mock_connections(device_id):
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
        mock_l1.assert_called_once_with(["device-1"], "connect_ports", rid, uid, mock_session)
        mock_l2.assert_called_once_with(["device-1"], "provision", rid, uid, mock_session)


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
        mock_l1.assert_called_once_with(["device-1"], "disconnect_ports", rid, uid, mock_session)
        mock_l2.assert_called_once_with(["device-1"], "deprovision", rid, uid, mock_session)


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
