"""The device-hold invariant for reservation status transitions (issue #897).

An exclusive device is RESERVED in inventory if and only if some reservation in
{PENDING_PROVISION, ACTIVE} holds it. A PENDING reservation holds nothing (since
#132 a future booking touches no inventory status until activation), so cancelling
it, PATCH-adding a device to it, and PATCH-removing a device from it must write NO
inventory status. This file enumerates the whole space: every pre-status in
{PENDING, PENDING_PROVISION, ACTIVE} crossed with every operation in
{cancel, PATCH-add, PATCH-remove}, asserting the EXACT list of inventory status
calls (a spy on `_update_device_statuses`, never a silent patch-out).
"""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from app.models.reservation import Reservation, ReservationStatus, TopologyType
from app.schemas.reservation import ReservationUpdate
from app.services.reservation_service import cancel_reservation, update_reservation

from tests._harness import TestSessionLocal

NOW = datetime.now(timezone.utc)
USER_ID = uuid.uuid4()
HELD = uuid.uuid4()
OTHER = uuid.uuid4()

PENDING = ReservationStatus.PENDING
PROVISION = ReservationStatus.PENDING_PROVISION
ACTIVE = ReservationStatus.ACTIVE

SVC = "app.services.reservation_service"


def _device(device_id, status="AVAILABLE"):
    return {
        "id": str(device_id),
        "name": f"dev-{str(device_id)[:8]}",
        "topology_type": "PHYSICAL",
        "status": status,
        "exclusive": True,
    }


async def _insert(db, status, device_ids):
    res = Reservation(
        user_id=USER_ID,
        device_ids=[str(d) for d in device_ids],
        topology_type=TopologyType.PHYSICAL,
        purpose="test",
        start_time=NOW + timedelta(hours=1),
        end_time=NOW + timedelta(hours=3),
        status=status,
    )
    db.add(res)
    await db.commit()
    await db.refresh(res)
    return res


@pytest.fixture
def spy():
    """Spy on the inventory status write and stub the other HTTP seams."""
    update = AsyncMock(return_value=[])

    async def fetch_best_effort(ids):
        return [_device(d) for d in ids]

    async def fetch(ids, token):
        return [_device(d) for d in ids]

    with (
        patch(f"{SVC}._update_device_statuses", new=update),
        patch(f"{SVC}._fetch_devices_best_effort", new=fetch_best_effort),
        patch(f"{SVC}._fetch_devices", new=fetch),
        patch(f"{SVC}._archive_reservation_fork_best_effort", new=AsyncMock()),
        patch(f"{SVC}._prune_removed_devices_from_fork_best_effort", new=AsyncMock()),
    ):
        yield update


def _calls(spy_mock):
    return [([str(d) for d in c.args[0]], c.args[1]) for c in spy_mock.call_args_list]


@pytest.mark.parametrize(
    "pre_status,expected_calls",
    [
        (PENDING, []),
        (PROVISION, [([str(HELD)], "AVAILABLE")]),
        (ACTIVE, [([str(HELD)], "AVAILABLE")]),
    ],
)
async def test_cancel_writes_inventory_only_when_row_held_devices(spy, pre_status, expected_calls):
    async with TestSessionLocal() as db:
        res = await _insert(db, pre_status, [HELD])
        out = await cancel_reservation(db, res.id, USER_ID, "token")
    assert out.status == ReservationStatus.CANCELLED
    assert _calls(spy) == expected_calls


@pytest.mark.parametrize(
    "pre_status,expected_calls",
    [
        (PENDING, []),
        (ACTIVE, [([str(OTHER)], "RESERVED")]),
    ],
)
async def test_patch_add_writes_inventory_only_when_row_holds_devices(
    spy, pre_status, expected_calls
):
    async with TestSessionLocal() as db:
        res = await _insert(db, pre_status, [HELD])
        out = await update_reservation(
            db, res.id, USER_ID, ReservationUpdate(device_ids=[HELD, OTHER]), token="t"
        )
    assert sorted(str(d) for d in out.device_ids) == sorted([str(HELD), str(OTHER)])
    assert _calls(spy) == expected_calls


@pytest.mark.parametrize(
    "pre_status,expected_calls",
    [
        (PENDING, []),
        (ACTIVE, [([str(OTHER)], "AVAILABLE")]),
    ],
)
async def test_patch_remove_writes_inventory_only_when_row_holds_devices(
    spy, pre_status, expected_calls
):
    async with TestSessionLocal() as db:
        res = await _insert(db, pre_status, [HELD, OTHER])
        out = await update_reservation(
            db, res.id, USER_ID, ReservationUpdate(device_ids=[HELD]), token="t"
        )
    assert [str(d) for d in out.device_ids] == [str(HELD)]
    assert _calls(spy) == expected_calls


@pytest.mark.parametrize("device_ids", [[HELD, OTHER], [HELD]], ids=["add", "remove"])
async def test_patch_on_pending_provision_is_refused_with_zero_inventory_calls(spy, device_ids):
    async with TestSessionLocal() as db:
        res = await _insert(db, PROVISION, [HELD] if len(device_ids) == 2 else [HELD, OTHER])
        with pytest.raises(ValueError, match="Cannot update a PENDING_PROVISION reservation"):
            await update_reservation(
                db, res.id, USER_ID, ReservationUpdate(device_ids=device_ids), token="t"
            )
    assert _calls(spy) == []


async def test_patch_add_still_refuses_a_device_that_is_not_available_on_pending(spy):
    """The AVAILABLE-only add refusal is unchanged for a PENDING row."""

    async def busy(ids, token):
        return [_device(d, status="RESERVED") for d in ids]

    async with TestSessionLocal() as db:
        res = await _insert(db, PENDING, [HELD])
        with (
            patch(f"{SVC}._fetch_devices", new=busy),
            pytest.raises(ValueError, match="not available"),
        ):
            await update_reservation(
                db, res.id, USER_ID, ReservationUpdate(device_ids=[HELD, OTHER]), token="t"
            )
    assert _calls(spy) == []
