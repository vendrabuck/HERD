"""Owner-only reservation routes, no admin bypass (issue #998).

By decision (docs/ROLES.md; issue #843 aligned the UI), GET /{id}, PATCH /{id},
and PUT /{id}/release answer only the reservation's owner. Every other caller,
an admin or superadmin included, gets the same 404 an unknown id gets, and
nothing changes. If that decision changes, these tests are where it shows.
Cancel is the one owner-or-admin action and is pinned elsewhere.
"""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from app.models.outbox import OutboxEvent
from app.models.reservation import Reservation, ReservationStatus, TopologyType
from sqlalchemy import select

from tests._harness import TestSessionLocal

OWNER = uuid.uuid4()
DEVICE = uuid.uuid4()
SVC = "app.services.reservation_service"


def _payload(role: str) -> dict:
    return {"sub": str(uuid.uuid4()), "username": f"not-owner-{role}", "role": role}


async def _insert_active() -> uuid.UUID:
    now = datetime.now(timezone.utc)
    rid = uuid.uuid4()
    async with TestSessionLocal() as s:
        s.add(
            Reservation(
                id=rid,
                user_id=OWNER,
                owner_name="owner",
                device_ids=[DEVICE],
                topology_type=TopologyType.PHYSICAL,
                purpose="owner purpose",
                start_time=now - timedelta(hours=1),
                end_time=now + timedelta(hours=2),
                status=ReservationStatus.ACTIVE,
            )
        )
        await s.commit()
    return rid


async def _row(rid) -> Reservation:
    async with TestSessionLocal() as s:
        return (await s.execute(select(Reservation).where(Reservation.id == rid))).scalar_one()


async def _outbox_count() -> int:
    async with TestSessionLocal() as s:
        return len((await s.execute(select(OutboxEvent))).scalars().all())


@pytest.fixture
def inventory():
    update = AsyncMock()
    with (
        patch(f"{SVC}._update_device_statuses", new=update),
        patch(f"{SVC}._fetch_devices", new=AsyncMock(return_value=[])),
        patch(f"{SVC}._fetch_devices_best_effort", new=AsyncMock(return_value=[])),
        patch(f"{SVC}._archive_reservation_fork_best_effort", new=AsyncMock()),
    ):
        yield update


async def _assert_untouched(rid, inventory):
    row = await _row(rid)
    assert row.status == ReservationStatus.ACTIVE
    assert row.purpose == "owner purpose"
    assert row.modified_by is None
    assert await _outbox_count() == 0
    inventory.assert_not_called()


@pytest.mark.parametrize("role", ["admin", "superadmin"])
async def test_non_owner_admin_get_by_id_is_404(make_client, inventory, role):
    rid = await _insert_active()
    async with make_client(_payload(role)) as client:
        resp = await client.get(f"/{rid}")
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Reservation not found"}


@pytest.mark.parametrize("role", ["admin", "superadmin"])
async def test_non_owner_admin_patch_is_404_and_changes_nothing(make_client, inventory, role):
    rid = await _insert_active()
    async with make_client(_payload(role)) as client:
        resp = await client.patch(f"/{rid}", json={"purpose": "admin rewrite"})
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Reservation not found"}
    await _assert_untouched(rid, inventory)


@pytest.mark.parametrize("role", ["admin", "superadmin"])
async def test_non_owner_admin_release_is_404_and_changes_nothing(make_client, inventory, role):
    rid = await _insert_active()
    async with make_client(_payload(role)) as client:
        resp = await client.put(f"/{rid}/release")
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Reservation not found"}
    await _assert_untouched(rid, inventory)


async def test_non_owner_user_patch_is_404_and_changes_nothing(make_client, inventory):
    rid = await _insert_active()
    async with make_client(_payload("user")) as client:
        resp = await client.patch(f"/{rid}", json={"purpose": "user rewrite"})
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Reservation not found"}
    await _assert_untouched(rid, inventory)


async def test_owner_patch_still_succeeds(make_client, inventory):
    """Control: the same PATCH from the owner lands, so the 404s above are the
    ownership rule and not a broken request."""
    rid = await _insert_active()
    owner = {"sub": str(OWNER), "username": "owner", "role": "user"}
    async with make_client(owner) as client:
        resp = await client.patch(f"/{rid}", json={"purpose": "owner rewrite"})
    assert resp.status_code == 200
    assert (await _row(rid)).purpose == "owner rewrite"
