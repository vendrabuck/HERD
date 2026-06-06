"""Additional tests for the L2 switch VLAN assignment and provisioning paths."""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from app.services import nats_consumer
from app.services.nats_consumer import (
    _assign_vlans_to_operations,
    _execute_l2_switch_operations,
)


class _FakeSessionCtx:
    async def __aenter__(self):
        return "fake-session"

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _fake_get_db_session():
    return _FakeSessionCtx()


# --- _assign_vlans_to_operations ---


@pytest.mark.asyncio
async def test_assign_vlans_groups_switches_by_fabric():
    fabric_x = uuid.uuid4()
    fabric_y = uuid.uuid4()
    ops = [
        {"switch_device_id": "sw-a", "switch_port": "eth1", "tag": "tagged"},
        {"switch_device_id": "sw-b", "switch_port": "eth2", "tag": "tagged"},
        {"switch_device_id": "sw-c", "switch_port": "eth3", "tag": "tagged"},
    ]

    fabrics = {"sw-a": fabric_x, "sw-b": fabric_x, "sw-c": fabric_y}

    async def _fake_fabric(sid: str) -> uuid.UUID:
        return fabrics[sid]

    vlan_counter = iter([100, 200])

    async def _fake_find_or_assign_vlan(db, res_id, fid, sids):
        return next(vlan_counter)

    with (
        patch(
            "app.services.vlan_service.fetch_fabric_id",
            new=AsyncMock(side_effect=_fake_fabric),
        ),
        patch(
            "app.services.vlan_service.find_or_assign_vlan",
            new=AsyncMock(side_effect=_fake_find_or_assign_vlan),
        ),
    ):
        result = await _assign_vlans_to_operations(ops, str(uuid.uuid4()), _fake_get_db_session)

    vlans_by_switch = {op["switch_device_id"]: op["vlan_id"] for op in result}
    # sw-a and sw-b share a fabric so they must share a VLAN
    assert vlans_by_switch["sw-a"] == vlans_by_switch["sw-b"]
    # sw-c is a different fabric, different VLAN
    assert vlans_by_switch["sw-c"] != vlans_by_switch["sw-a"]


@pytest.mark.asyncio
async def test_assign_vlans_uses_fallback_when_fabric_lookup_fails(caplog):
    ops = [{"switch_device_id": "sw-a", "switch_port": "eth1", "tag": "tagged"}]

    async def _fake_find_or_assign_vlan(db, res_id, fid, sids):
        return 42

    with (
        patch(
            "app.services.vlan_service.fetch_fabric_id",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "app.services.vlan_service.find_or_assign_vlan",
            new=AsyncMock(side_effect=_fake_find_or_assign_vlan),
        ),
    ):
        result = await _assign_vlans_to_operations(ops, str(uuid.uuid4()), _fake_get_db_session)
    assert result[0]["vlan_id"] == 42
    assert any("Could not determine fabric" in rec.message for rec in caplog.records)


# --- _execute_l2_switch_operations early-returns ---


@pytest.mark.asyncio
async def test_execute_l2_operations_skips_without_reservation_id(caplog):
    await _execute_l2_switch_operations(
        device_ids=["dev-1"],
        l2_action="provision",
        reservation_id=None,
        user_id=str(uuid.uuid4()),
        get_db_session=_fake_get_db_session,
    )
    assert any("No reservation_id for L2 operations" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_execute_l2_operations_noops_when_no_l2_ops(caplog):
    """If no L2 switch operations are resolved, provisioning is a no-op."""
    import logging

    caplog.set_level(logging.INFO, logger="app.services.nats_consumer")
    with patch(
        "app.services.nats_consumer._resolve_l2_switch_operations",
        new=AsyncMock(return_value=[]),
    ):
        await _execute_l2_switch_operations(
            device_ids=["dev-1"],
            l2_action="provision",
            reservation_id=str(uuid.uuid4()),
            user_id=str(uuid.uuid4()),
            get_db_session=_fake_get_db_session,
        )
    assert any("No L2 switch operations needed" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_execute_l2_operations_deprovision_uses_stored_assignments():
    """Deprovisioning with stored assignments maps each op to its stored VLAN."""
    ops = [
        {"switch_device_id": "sw-a", "switch_port": "eth1", "tag": "tagged"},
        {"switch_device_id": "sw-c", "switch_port": "eth3", "tag": "tagged"},
    ]

    class _Assignment:
        def __init__(self, vlan_id: int, sids: list[str]):
            self.vlan_id = vlan_id
            self.switch_device_ids = sids

    assignments = [_Assignment(101, ["sw-a"]), _Assignment(202, ["sw-b"])]

    captured_ops: list[dict] = []

    with (
        patch(
            "app.services.nats_consumer._resolve_l2_switch_operations",
            new=AsyncMock(return_value=ops),
        ),
        patch(
            "app.services.vlan_service.release_vlan",
            new=AsyncMock(return_value=assignments),
        ),
        # Short-circuit the provisioning path we don't want to exercise here.
        patch(
            "app.services.nats_consumer._fetch_device",
            new=AsyncMock(return_value=None),
        ),
    ):

        async def _capture(ops_in, *args, **kwargs):
            captured_ops.extend(ops_in)
            return ops_in

        await _execute_l2_switch_operations(
            device_ids=["dev-1"],
            l2_action="deprovision",
            reservation_id=str(uuid.uuid4()),
            user_id=str(uuid.uuid4()),
            get_db_session=_fake_get_db_session,
        )

    # sw-a has a stored assignment (101); sw-c has none so falls back to derived
    by_switch = {op["switch_device_id"]: op["vlan_id"] for op in ops}
    assert by_switch["sw-a"] == 101
    assert isinstance(by_switch["sw-c"], int)


@pytest.mark.asyncio
async def test_execute_l2_operations_deprovision_legacy_fallback_vlan():
    """When no assignments exist, deprovision uses a derived legacy VLAN."""
    res_id = str(uuid.uuid4())
    expected = nats_consumer._derive_vlan_id(res_id)
    ops = [
        {"switch_device_id": "sw-a", "switch_port": "eth1", "tag": "tagged"},
    ]

    with (
        patch(
            "app.services.nats_consumer._resolve_l2_switch_operations",
            new=AsyncMock(return_value=ops),
        ),
        patch(
            "app.services.vlan_service.release_vlan",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "app.services.nats_consumer._fetch_device",
            new=AsyncMock(return_value=None),
        ),
    ):
        await _execute_l2_switch_operations(
            device_ids=["dev-1"],
            l2_action="deprovision",
            reservation_id=res_id,
            user_id=str(uuid.uuid4()),
            get_db_session=_fake_get_db_session,
        )
    assert ops[0]["vlan_id"] == expected
