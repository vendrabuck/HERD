"""The Santa Clara ACL fixtures: a scoped user, an unscoped admin, one group."""

import httpx

from .groups import (
    add_group_member,
    bulk_add_devices_to_group,
    bulk_add_permissions_to_device_group,
    get_or_create_device_group,
    get_or_create_group,
)
from .inventory import get_or_create_device, pick_dut_template
from .users import register_acl_user

# --- ACL test fixtures (dedicated, scoped-visibility demo) -------------------
# A regular user ("scuser") that can see only a single "Santa Clara" device
# group, plus an unscoped admin ("scadmin") that sees everything. Used to
# exercise non-admin device-group ACL filtering by hand. Kept separate from the
# bulk users so it does not entangle with the 1000-user load fixtures.
ACL_TEST_USERS = [
    {"username": "scuser", "email": "scuser@herd.dev", "password": "scuser123", "role": None},
    {"username": "scadmin", "email": "scadmin@herd.dev", "password": "scadmin123", "role": "admin"},
]
SANTA_CLARA_DEVICE_GROUP = "Santa Clara"
SANTA_CLARA_USER_GROUP = "Santa Clara Techs"
SANTA_CLARA_DEVICE_COUNT = 3


def seed_acl_test_fixtures(client: httpx.Client) -> None:
    """Stage the Santa Clara ACL demo: scoped user, unscoped admin, one device group."""
    print("\n--- ACL Test Fixtures (Santa Clara) ---")
    template_id = pick_dut_template(client)
    if not template_id:
        print("  No device template available; skipping ACL fixtures")
        return

    # Dedicated devices, grouped only under Santa Clara, so scuser sees exactly these.
    device_ids = []
    for i in range(1, SANTA_CLARA_DEVICE_COUNT + 1):
        name = f"santa-clara-{i:02d}"
        device_ids.append(get_or_create_device(client, name, template_id, ip=f"10.70.0.{i}"))
    print(f"  {len(device_ids)} Santa Clara devices")

    dg_id = get_or_create_device_group(
        client, SANTA_CLARA_DEVICE_GROUP, "Santa Clara lab resources (ACL test)"
    )
    bulk_add_devices_to_group(client, dg_id, device_ids)

    ug_id = get_or_create_group(
        client, SANTA_CLARA_USER_GROUP, "Santa Clara technicians (ACL test)"
    )
    bulk_add_permissions_to_device_group(client, dg_id, [ug_id])

    scuser_id = None
    for spec in ACL_TEST_USERS:
        uid = register_acl_user(client, spec)
        if uid and spec["username"] == "scuser":
            scuser_id = uid
    if scuser_id:
        add_group_member(client, ug_id, scuser_id)
        print(f"  Added scuser to {SANTA_CLARA_USER_GROUP}")
    print(
        "  ACL fixtures ready. Login by email: scuser@herd.dev / scuser123 "
        f"(sees only {SANTA_CLARA_DEVICE_GROUP}); scadmin@herd.dev / scadmin123 (admin, sees all)."
    )
