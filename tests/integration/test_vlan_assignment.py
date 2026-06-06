"""Integration tests for fabric-aware VLAN assignment (best effort).

VLAN provisioning is driven by NATS events to the execution service, which
runs user-authored L2 switch driver code in a subprocess sandbox. Verifying
the full lifecycle (assign on reservation.created, release on cancel/complete)
requires a mock L2 switch + a test driver package that does not need real
hardware.

Neither fixture currently exists, so these tests are intentionally skipped.
Remove the skipif and implement the mock driver harness when the L2 test
infrastructure is in place.
"""

import os

import pytest

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("HERD_VLAN_TEST_DRIVER_ID") is None,
        reason="VLAN assignment tests require HERD_VLAN_TEST_DRIVER_ID env var "
        "pointing to a mock L2 driver",
    ),
]


async def test_vlan_assigned_on_reservation_create_with_l2_switch(admin_client):
    """TODO: create a reservation that includes an L2 switch path and verify
    a VlanAssignment row exists with status=ACTIVE for the reservation's fabric.
    """
    pytest.skip("Pending mock L2 driver harness")


async def test_vlan_released_on_reservation_cancel(admin_client):
    """TODO: cancel an L2 reservation and verify the VlanAssignment row flips
    to status=RELEASED.
    """
    pytest.skip("Pending mock L2 driver harness")


async def test_vlan_ids_are_unique_within_same_fabric(admin_client):
    """TODO: create two overlapping L2 reservations in the same fabric and
    verify they receive different VLAN IDs (no conflict).
    """
    pytest.skip("Pending mock L2 driver harness")
