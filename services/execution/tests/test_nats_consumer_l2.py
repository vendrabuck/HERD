"""Additional tests for the L2 switch VLAN assignment and provisioning paths."""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from app.database import Base
from app.models.execution_run import ExecutionRun
from app.services import nats_consumer
from app.services.nats_consumer import (
    _assign_vlans_to_operations,
    _execute_l2_switch_operations,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool


class _FakeResult:
    def scalar_one_or_none(self):
        # No allocation row in the fake DB: _assign_vlans_to_operations leaves
        # vlan_assignment_id None (ADR 0009 phase 4), so the ledger write is skipped
        # and these VLAN-grouping unit tests stay focused on the vlan_id mapping.
        return None


class _FakeSession:
    async def execute(self, *args, **kwargs):
        return _FakeResult()


class _FakeSessionCtx:
    async def __aenter__(self):
        return _FakeSession()

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _fake_get_db_session():
    return _FakeSessionCtx()


# --- issue #393: result gating (transport success, driver-reported failure) ---
#
# These tests need a real DB session (create_execution_run/update_execution_run
# do real ORM writes), unlike the fake-session tests above that short-circuit
# before reaching the driver-call block.

_result_gating_engine = create_async_engine(
    "sqlite+aiosqlite:///:memory:",
    echo=False,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_ResultGatingSessionLocal = async_sessionmaker(_result_gating_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def _result_gating_db():
    async with _result_gating_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _result_gating_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


def _real_db_session_factory():
    """get_db_session() must directly return an async context manager."""

    class _Ctx:
        async def __aenter__(self):
            self._session = _ResultGatingSessionLocal()
            return self._session

        async def __aexit__(self, *args):
            await self._session.close()

    def _get():
        return _Ctx()

    return _get


_L2_SWITCH_ID = str(uuid.uuid4())
_L2_DRIVER_ID = str(uuid.uuid4())
_L2_USER_ID = str(uuid.uuid4())
_L2_RES_ID = str(uuid.uuid4())

_L2_SWITCH_DATA = {
    "id": _L2_SWITCH_ID,
    "name": "L2-Switch",
    "template_id": "tmpl-1",
    "driver_id": _L2_DRIVER_ID,
    "driver_sha256": "sha256abc",
    "driver_filename": "driver.zip",
    "connection_type": "Layer 2 Switch",
    "field_data": {},
}

_L2_TEMPLATE_DATA = {
    "id": "tmpl-1",
    "name": "L2 Template",
    "sections": [],
}

_L2_SUCCESS_RESULT = {"success": True, "output": {"result": True}, "error": None, "duration_ms": 5}


def _l2_result_gating_patches(execute_fn, ops, assignments=None):
    """Patch set driving _execute_l2_switch_operations end to end for one switch."""
    patches = [
        patch(
            "app.services.nats_consumer._resolve_l2_switch_operations",
            new=AsyncMock(return_value=ops),
        ),
        patch(
            "app.services.nats_consumer._fetch_device", new=AsyncMock(return_value=_L2_SWITCH_DATA)
        ),
        patch(
            "app.services.nats_consumer._fetch_template",
            new=AsyncMock(return_value=_L2_TEMPLATE_DATA),
        ),
        patch("app.services.driver_loader.load_driver", new=AsyncMock(return_value="/tmp/driver")),
        patch("app.services.driver_sandbox.execute_driver_method", side_effect=execute_fn),
    ]
    if assignments is not None:
        patches.append(
            patch(
                "app.services.vlan_service.release_vlan",
                new=AsyncMock(return_value=assignments),
            )
        )
    else:
        patches.append(
            patch(
                "app.services.vlan_service.find_or_assign_vlan",
                new=AsyncMock(return_value=100),
            )
        )
        patches.append(
            patch(
                "app.services.vlan_service.fetch_fabric_id",
                new=AsyncMock(return_value=uuid.uuid4()),
            )
        )
    return patches


async def _run_by_action(action: str) -> list[ExecutionRun]:
    async with _ResultGatingSessionLocal() as db:
        result = await db.execute(select(ExecutionRun).where(ExecutionRun.action == action))
        return list(result.scalars().all())


@pytest.mark.asyncio
async def test_execute_l2_provision_create_vlan_semantic_result_failure_records_failed():
    """create_vlan returning {"success": True, "output": {"success": False}}
    (transport ok, driver-reported failure) records FAILED with the driver's
    error, not SUCCESS (issue #393, the L2 analogue of #370)."""
    ops = [{"switch_device_id": _L2_SWITCH_ID, "switch_port": "eth1", "tag": "tagged"}]

    def _semantic_fail(driver_path, action, context, **kwargs):
        if action == "create_vlan":
            return {
                "success": True,
                "output": {"success": False, "error": "vlan pool exhausted"},
                "duration_ms": 4,
            }
        return _L2_SUCCESS_RESULT

    patches = _l2_result_gating_patches(_semantic_fail, ops)
    for p in patches:
        p.start()
    try:
        await _execute_l2_switch_operations(
            device_ids=["dut-1"],
            l2_action="provision",
            reservation_id=_L2_RES_ID,
            user_id=_L2_USER_ID,
            get_db_session=_real_db_session_factory(),
        )
    finally:
        for p in patches:
            p.stop()

    runs = await _run_by_action("create_vlan")
    assert runs and all(r.status == "FAILED" for r in runs)
    assert all(r.error == "vlan pool exhausted" for r in runs)


@pytest.mark.asyncio
async def test_execute_l2_provision_add_to_vlan_semantic_result_failure_records_failed():
    """add_to_vlan returning a driver-reported failure records FAILED with the
    driver's error (issue #393)."""
    ops = [{"switch_device_id": _L2_SWITCH_ID, "switch_port": "eth1", "tag": "tagged"}]

    def _semantic_fail(driver_path, action, context, **kwargs):
        if action == "add_to_vlan":
            return {
                "success": True,
                "output": {"success": False, "error": "port admin-down"},
                "duration_ms": 4,
            }
        return _L2_SUCCESS_RESULT

    patches = _l2_result_gating_patches(_semantic_fail, ops)
    for p in patches:
        p.start()
    try:
        await _execute_l2_switch_operations(
            device_ids=["dut-1"],
            l2_action="provision",
            reservation_id=_L2_RES_ID,
            user_id=_L2_USER_ID,
            get_db_session=_real_db_session_factory(),
        )
    finally:
        for p in patches:
            p.stop()

    runs = await _run_by_action("add_to_vlan")
    assert runs and all(r.status == "FAILED" for r in runs)
    assert all(r.error == "port admin-down" for r in runs)


@pytest.mark.asyncio
async def test_execute_l2_deprovision_remove_from_vlan_semantic_result_failure_records_failed():
    """remove_from_vlan returning a driver-reported failure records FAILED with
    the driver's error (issue #393)."""

    class _Assignment:
        def __init__(self, vlan_id, sids):
            self.vlan_id = vlan_id
            self.switch_device_ids = sids

    ops = [{"switch_device_id": _L2_SWITCH_ID, "switch_port": "eth1", "tag": "tagged"}]
    assignments = [_Assignment(404, [_L2_SWITCH_ID])]

    def _semantic_fail(driver_path, action, context, **kwargs):
        if action == "remove_from_vlan":
            return {
                "success": True,
                "output": {"success": False, "error": "port not in vlan"},
                "duration_ms": 4,
            }
        return _L2_SUCCESS_RESULT

    patches = _l2_result_gating_patches(_semantic_fail, ops, assignments=assignments)
    for p in patches:
        p.start()
    try:
        await _execute_l2_switch_operations(
            device_ids=["dut-1"],
            l2_action="deprovision",
            reservation_id=_L2_RES_ID,
            user_id=_L2_USER_ID,
            get_db_session=_real_db_session_factory(),
        )
    finally:
        for p in patches:
            p.stop()

    runs = await _run_by_action("remove_from_vlan")
    assert runs and all(r.status == "FAILED" for r in runs)
    assert all(r.error == "port not in vlan" for r in runs)


@pytest.mark.asyncio
async def test_execute_l2_deprovision_delete_vlan_semantic_result_failure_records_failed():
    """delete_vlan returning a driver-reported failure records FAILED with the
    driver's error (issue #393)."""

    class _Assignment:
        def __init__(self, vlan_id, sids):
            self.vlan_id = vlan_id
            self.switch_device_ids = sids

    ops = [{"switch_device_id": _L2_SWITCH_ID, "switch_port": "eth1", "tag": "tagged"}]
    assignments = [_Assignment(404, [_L2_SWITCH_ID])]

    def _semantic_fail(driver_path, action, context, **kwargs):
        if action == "delete_vlan":
            return {
                "success": True,
                "output": {"success": False, "error": "vlan still has members"},
                "duration_ms": 4,
            }
        return _L2_SUCCESS_RESULT

    patches = _l2_result_gating_patches(_semantic_fail, ops, assignments=assignments)
    for p in patches:
        p.start()
    try:
        await _execute_l2_switch_operations(
            device_ids=["dut-1"],
            l2_action="deprovision",
            reservation_id=_L2_RES_ID,
            user_id=_L2_USER_ID,
            get_db_session=_real_db_session_factory(),
        )
    finally:
        for p in patches:
            p.stop()

    runs = await _run_by_action("delete_vlan")
    assert runs and all(r.status == "FAILED" for r in runs)
    assert all(r.error == "vlan still has members" for r in runs)


@pytest.mark.asyncio
async def test_execute_l2_provision_bare_data_output_stays_success():
    """An output dict with no `success` key is a bare-data return and stays
    SUCCESS, matching the conservative #370 posture."""
    ops = [{"switch_device_id": _L2_SWITCH_ID, "switch_port": "eth1", "tag": "tagged"}]

    def _bare_data(driver_path, action, context, **kwargs):
        if action == "create_vlan":
            return {
                "success": True,
                "output": {"vlan_created": True},
                "duration_ms": 4,
            }
        return _L2_SUCCESS_RESULT

    patches = _l2_result_gating_patches(_bare_data, ops)
    for p in patches:
        p.start()
    try:
        await _execute_l2_switch_operations(
            device_ids=["dut-1"],
            l2_action="provision",
            reservation_id=_L2_RES_ID,
            user_id=_L2_USER_ID,
            get_db_session=_real_db_session_factory(),
        )
    finally:
        for p in patches:
            p.stop()

    runs = await _run_by_action("create_vlan")
    assert runs and all(r.status == "SUCCESS" for r in runs)


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
