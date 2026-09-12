"""Command-line entry point: `python -m seedtools <subcommand>`.

Subcommands:
  full  the standard demo population (users, drivers, templates, devices,
        ports, L1/L2 switches, cabling, groups, topologies, ACL fixtures),
        with SEED_FRR=1 / SEED_NOS=1 layering the two lab demos on top.
  acl   the Santa Clara ACL fixtures only (the retired --acl-only).
  frr   the FRR live-config demo only; --full runs the whole population with
        it layered on, which is what scripts/seed_frr_demo.sh did.
  nos   the NOS test lab only (the retired --nos-only); --full runs the whole
        population with it layered on, which is what
        scripts/seed_nos_lab.sh --full did.

Every subcommand is re-runnable: the seed is get-or-create throughout, so
existing resources are skipped.
"""

import argparse
import os
import sys

import httpx

from .acl_fixtures import seed_acl_test_fixtures
from .cabling import create_connections, create_l2_connections
from .catalog import (
    DEFAULT_PORT_COUNT,
    DEVICE_TEMPLATES,
    DEVICES,
    ISOLATED_DEMO_IP_BASE,
    L1_PORTS_TOTAL,
    L1_SWITCHES_PER_LAB,
    L2_PORTS_TOTAL,
    L2_SWITCHES_PER_LAB,
    NUM_ADMINS,
    NUM_EDGE_SWITCHES,
    NUM_HUB_SWITCHES,
    NUM_ISOLATED_DEMO_DEVICES,
    NUM_L1_SWITCHES,
    NUM_L2_HUB_SWITCHES,
    NUM_L2_SWITCHES,
    NUM_USERS,
    POLL_INTERVAL_SECONDS,
    PORT_SECTIONS,
    SECTIONS,
    TEMPLATE_PORT_COUNTS,
    TOTAL_DEVICES,
)
from .client import BASE, EMAIL, login
from .drivers import (
    _make_cisco_6509_driver_zip,
    _make_l1_driver_zip,
    _make_l2_driver_zip,
    _make_management_demo_driver_zip,
    get_or_create_driver,
)
from .frr_demo import seed_frr_demo
from .groups import (
    add_group_member,
    bulk_add_devices_to_group,
    bulk_add_permissions_to_device_group,
    bulk_remove_devices_from_group,
    bulk_remove_from_group,
    get_all_user_ids,
    get_or_create_device_group,
    get_or_create_group,
)
from .inventory import (
    create_l1_switch_ports,
    create_ports,
    get_or_create_device,
    get_or_create_template,
)
from .nos_lab import seed_nos_lab
from .topologies import seed_topologies
from .users import create_users


def connect() -> httpx.Client:
    """Build an httpx client already carrying a superadmin bearer token."""
    client = httpx.Client(verify=False, timeout=30.0)
    token = login(client)
    client.headers["Authorization"] = f"Bearer {token}"
    return client


def run_full(client: httpx.Client, seed_frr: bool = False, seed_nos: bool = False) -> None:
    """The standard demo population. Was seed_devices_public.py's main()."""
    # Users
    print("\n--- Users ---")
    create_users(client)

    # Drivers
    print("\n--- Drivers ---")
    management_demo_zip = _make_management_demo_driver_zip()
    network_driver_id = get_or_create_driver(
        client, "Network OS Management", "Management", zip_bytes=management_demo_zip
    )
    endpoint_driver_id = get_or_create_driver(
        client, "Endpoint Management", "Management", zip_bytes=management_demo_zip
    )
    l1_driver_id = get_or_create_driver(
        client,
        "L1 Switch Driver",
        "Layer 1 Switch",
        zip_bytes=_make_l1_driver_zip(),
    )
    l2_driver_id = get_or_create_driver(
        client,
        "L2 Switch Driver",
        "Layer 2 Switch",
        zip_bytes=_make_l2_driver_zip(),
    )
    cisco_6509_driver_id = get_or_create_driver(
        client,
        "Cisco 6509 L2 Driver",
        "Layer 2 Switch",
        zip_bytes=_make_cisco_6509_driver_zip(),
    )

    # Map template names to driver ids
    CLIENT_TEMPLATE_NAMES = {
        "Windows 10 Client",
        "Windows 11 Client",
        "macOS Client",
        "Ubuntu Client",
    }

    # Templates
    print("\n--- Templates ---")
    template_ids: dict[str, str] = {}
    for tmpl in DEVICE_TEMPLATES:
        driver_id = (
            endpoint_driver_id if tmpl["name"] in CLIENT_TEMPLATE_NAMES else network_driver_id
        )
        template_ids[tmpl["name"]] = get_or_create_template(
            client,
            tmpl["name"],
            "device",
            tmpl["description"],
            tmpl["sections"],
            driver_id=driver_id,
            vendor=tmpl["vendor"],
            model=tmpl["model"],
        )

    # L1 switch template (non-exclusive: shared infrastructure)
    l1_template_id = get_or_create_template(
        client,
        "L1-Switch-256",
        "device",
        "Layer 1 switch with 256 backplane ports (8 slots x 32 ports)",
        SECTIONS,
        driver_id=l1_driver_id,
        exclusive=False,
        vendor="Generic",
        model="L1-Switch-256",
    )

    # L2 switch template (non-exclusive: shared infrastructure). No
    # template-level poll cadence: health polling for the screenshot demo is
    # opt-in per-device on a curated subset (see POLL_SUBSET_COUNTS and the L2
    # hub devices enrolled below), so a fresh environment populates badges
    # within minutes instead of polling every L2 edge switch.
    l2_template_id = get_or_create_template(
        client,
        "L2-Switch-48",
        "device",
        "Layer 2 switch with 48 access ports",
        SECTIONS,
        driver_id=l2_driver_id,
        exclusive=False,
        vendor="Generic",
        model="L2-Switch-48",
    )

    # Cisco Catalyst 6509 demo template (non-exclusive: shared infrastructure)
    cisco_6509_template_id = get_or_create_template(  # noqa: F841
        client,
        "Cisco-Catalyst-6509",
        "device",
        "Cisco Catalyst 6509 Layer 2 switch (demo, 48 GigabitEthernet ports on slot 1)",
        SECTIONS,
        driver_id=cisco_6509_driver_id,
        exclusive=False,
        vendor="Cisco",
        model="Catalyst 6509",
    )

    port_template_id = get_or_create_template(
        client, "1Gb Copper", "port", "1Gb copper Ethernet port", PORT_SECTIONS
    )

    # DUT Devices
    print("\n--- DUT Devices ---")
    device_ids: list[str] = []
    pa_device_ids: list[str] = []
    client_device_ids: list[str] = []
    total_devs = len(DEVICES)
    for i, dev in enumerate(DEVICES, 1):
        did = get_or_create_device(
            client,
            dev["name"],
            template_ids[dev["template"]],
            dev["ip"],
            poll_interval_seconds=dev.get("poll_interval_seconds"),
        )
        device_ids.append(did)
        if dev["template"] in CLIENT_TEMPLATE_NAMES:
            client_device_ids.append(did)
        else:
            pa_device_ids.append(did)
        if i % 100 == 0 or i == total_devs:
            print(f"  Devices: {i}/{total_devs}")
    print(f"  PA devices: {len(pa_device_ids)}, client devices: {len(client_device_ids)}")

    # L1 Switch Devices
    print("\n--- L1 Switch Devices ---")
    edge_switch_ids: list[str] = []
    for i in range(1, NUM_EDGE_SWITCHES + 1):
        name = f"L1-Edge-{i:02d}"
        ip = f"172.16.0.{i}"
        did = get_or_create_device(client, name, l1_template_id, ip)
        edge_switch_ids.append(did)
    print(f"  Created {len(edge_switch_ids)} edge switches")

    hub_switch_ids: list[str] = []
    for i in range(1, NUM_HUB_SWITCHES + 1):
        name = f"L1-Hub-{i:02d}"
        ip = f"172.16.1.{i}"
        did = get_or_create_device(
            client, name, l1_template_id, ip, poll_interval_seconds=POLL_INTERVAL_SECONDS
        )
        hub_switch_ids.append(did)
    print(f"  Created {len(hub_switch_ids)} hub switches")

    all_l1_ids = edge_switch_ids + hub_switch_ids

    # L2 Switch Devices
    print("\n--- L2 Switch Devices ---")
    l2_switch_ids: list[str] = []
    for i in range(1, NUM_L2_SWITCHES + 1):
        name = f"L2-Edge-{i:02d}"
        ip = f"172.16.2.{i}"
        did = get_or_create_device(client, name, l2_template_id, ip)
        l2_switch_ids.append(did)
    print(f"  Created {len(l2_switch_ids)} L2 edge switches")

    l2_hub_switch_ids: list[str] = []
    for i in range(1, NUM_L2_HUB_SWITCHES + 1):
        name = f"L2-Hub-{i:02d}"
        ip = f"172.16.3.{i}"
        did = get_or_create_device(
            client, name, l2_template_id, ip, poll_interval_seconds=POLL_INTERVAL_SECONDS
        )
        l2_hub_switch_ids.append(did)
    print(f"  Created {len(l2_hub_switch_ids)} L2 hub switches")

    all_l2_ids = l2_switch_ids + l2_hub_switch_ids

    # Isolated demo devices (zero cabling): known-unreachable endpoints for the
    # invalid-topology demos. They reuse an existing DUT template and are kept out
    # of every ports/cabling/group pass below, so they stay at zero connections.
    print("\n--- Isolated Demo Devices ---")
    isolated_device_ids: list[str] = []
    isolated_template_id = template_ids[DEVICE_TEMPLATES[0]["name"]]
    for i in range(1, NUM_ISOLATED_DEMO_DEVICES + 1):
        name = f"Isolated-Demo-{i:02d}"
        ip = f"{ISOLATED_DEMO_IP_BASE}{i}"
        did = get_or_create_device(client, name, isolated_template_id, ip)
        isolated_device_ids.append(did)
    print(f"  Created {len(isolated_device_ids)} isolated demo devices")

    # DUT Ports (variable count per template type)
    print("\n--- DUT Ports ---")
    for i, (did, dev) in enumerate(zip(device_ids, DEVICES), 1):
        port_count = TEMPLATE_PORT_COUNTS.get(dev["template"], DEFAULT_PORT_COUNT)
        create_ports(client, did, port_template_id, count=port_count, prefix="eth")
        if i % 100 == 0 or i == len(device_ids):
            print(f"  Ports: {i}/{len(device_ids)} devices processed")

    # L1 Switch Ports (256 backplane ports per switch)
    print("\n--- L1 Switch Ports ---")
    for i, sw_id in enumerate(all_l1_ids, 1):
        create_l1_switch_ports(client, sw_id, port_template_id)
        if i % 10 == 0 or i == len(all_l1_ids):
            print(f"  L1 switch ports: {i}/{len(all_l1_ids)} switches processed")

    # L2 Switch Ports (48 ports per switch: edges + hubs)
    print("\n--- L2 Switch Ports ---")
    for i, sw_id in enumerate(all_l2_ids, 1):
        create_ports(client, sw_id, port_template_id, count=L2_PORTS_TOTAL, prefix="eth")
        print(f"  L2 switch ports: {i}/{len(all_l2_ids)} switches processed")

    # Cabling connections (L1)
    print("\n--- L1 Cabling Connections ---")
    num_l1_connections = create_connections(
        client, pa_device_ids, client_device_ids, edge_switch_ids, hub_switch_ids
    )
    print(f"  Total L1 connections created: {num_l1_connections}")

    # Cabling connections (L2)
    print("\n--- L2 Cabling Connections ---")
    num_l2_connections = create_l2_connections(
        client,
        pa_device_ids,
        client_device_ids,
        l2_switch_ids,
        l2_hub_switch_ids,
    )
    print(f"  Total L2 connections created: {num_l2_connections}")
    num_connections = num_l1_connections + num_l2_connections

    # Lab topologies (demo/testing): requires the cabling fabric to exist
    print("\n--- Lab Topologies ---")
    num_valid_topos, num_invalid_topos = seed_topologies(
        client,
        edge_switch_ids,
        hub_switch_ids,
        l2_switch_ids,
        l2_hub_switch_ids,
        isolated_device_ids,
    )

    # Device groups
    print("\n--- Device Groups ---")
    dg_names = ["Lab Alpha", "Lab Bravo", "Lab Charlie"]
    dg_descriptions = [
        "Lab Alpha equipment",
        "Lab Bravo equipment",
        "Lab Charlie equipment",
    ]
    dg_ids = []
    for name, desc in zip(dg_names, dg_descriptions):
        dg_ids.append(get_or_create_device_group(client, name, desc))

    # Split PA devices evenly across 3 device groups
    pa_splits: list[list[str]] = [[], [], []]
    for idx, did in enumerate(pa_device_ids):
        pa_splits[idx % 3].append(did)

    # Split client devices evenly across 3 device groups
    client_splits: list[list[str]] = [[], [], []]
    for idx, did in enumerate(client_device_ids):
        client_splits[idx % 3].append(did)

    # Split L1 edge switches across labs (9 per lab)
    edge_splits: list[list[str]] = [
        edge_switch_ids[0:L1_SWITCHES_PER_LAB],
        edge_switch_ids[L1_SWITCHES_PER_LAB : L1_SWITCHES_PER_LAB * 2],
        edge_switch_ids[L1_SWITCHES_PER_LAB * 2 : L1_SWITCHES_PER_LAB * 3],
    ]

    # Split L2 edge switches across labs (10 per lab)
    l2_splits: list[list[str]] = [
        l2_switch_ids[0:L2_SWITCHES_PER_LAB],
        l2_switch_ids[L2_SWITCHES_PER_LAB : L2_SWITCHES_PER_LAB * 2],
        l2_switch_ids[L2_SWITCHES_PER_LAB * 2 : L2_SWITCHES_PER_LAB * 3],
    ]

    for i, (dg_id, name) in enumerate(zip(dg_ids, dg_names)):
        combined = (
            pa_splits[i]
            + client_splits[i]
            + edge_splits[i]
            + hub_switch_ids
            + l2_splits[i]
            + l2_hub_switch_ids
        )
        pa_n = len(pa_splits[i])
        cl_n = len(client_splits[i])
        l1_n = len(edge_splits[i]) + len(hub_switch_ids)
        l2_n = len(l2_splits[i]) + len(l2_hub_switch_ids)
        print(
            f"  {name}: {pa_n} PA + {cl_n} client + {l1_n} L1 + {l2_n} L2 switches "
            f"= {len(combined)} devices"
        )
        bulk_add_devices_to_group(client, dg_id, combined)

    # User groups
    print("\n--- User Groups ---")
    lab_admins_id = get_or_create_group(client, "Lab Admins", "Lab administration team")
    lab_managers_id = get_or_create_group(client, "Lab Managers", "Lab management team")
    sclab_id = get_or_create_group(client, "Lab Alpha Tier 1", "Lab Alpha tier 1 access")
    plano_id = get_or_create_group(client, "Lab Bravo Tier 1", "Lab Bravo tier 1 access")
    almere_id = get_or_create_group(client, "Lab Charlie Tier 1", "Lab Charlie tier 1 access")
    tier1_groups = [sclab_id, plano_id, almere_id]

    # Add members
    print("\n--- Group Members ---")
    user_map = get_all_user_ids(client)

    # Admins 1-40 in Lab Admins, admins 41-50 in Lab Managers
    lab_admin_count = 0
    for i in range(1, 41):
        uid = user_map.get(f"admin{i}")
        if uid:
            add_group_member(client, lab_admins_id, uid)
            lab_admin_count += 1
    print(f"  Added {lab_admin_count} admins to Lab Admins")

    lab_manager_count = 0
    for i in range(41, NUM_ADMINS + 1):
        uid = user_map.get(f"admin{i}")
        if uid:
            add_group_member(client, lab_managers_id, uid)
            lab_manager_count += 1
    print(f"  Added {lab_manager_count} admins to Lab Managers")

    # Split 1000 users evenly across 3 tier 1 groups (334, 333, 333)
    tier1_counts = [0, 0, 0]
    for i in range(1, NUM_USERS + 1):
        uid = user_map.get(f"user{i}")
        if uid:
            group_idx = (i - 1) % 3
            add_group_member(client, tier1_groups[group_idx], uid)
            tier1_counts[group_idx] += 1
    tier1_names = ["Lab Alpha Tier 1", "Lab Bravo Tier 1", "Lab Charlie Tier 1"]
    for name, count in zip(tier1_names, tier1_counts):
        print(f"  Added {count} users to {name}")

    # Remove all assigned users from "Not Grouped" (idempotent for re-runs)
    print("\n--- Remove from Not Grouped ---")
    not_grouped_id = get_or_create_group(
        client,
        "Not Grouped",
        "Default group for unassigned users",
    )
    all_assigned_uids = [
        uid
        for uname, uid in user_map.items()
        if uname.startswith("admin") or uname.startswith("user")
    ]
    print(f"  Removing {len(all_assigned_uids)} users from Not Grouped")
    bulk_remove_from_group(client, not_grouped_id, all_assigned_uids)

    # Remove all assigned devices from "No Pool" (idempotent for re-runs)
    print("\n--- Remove from No Pool ---")
    no_pool_id = get_or_create_device_group(
        client,
        "No Pool",
        "Default group for unassigned devices",
    )
    all_device_ids = device_ids + all_l1_ids + l2_switch_ids
    print(f"  Removing {len(all_device_ids)} devices from No Pool")
    bulk_remove_devices_from_group(client, no_pool_id, all_device_ids)

    # Device group permissions: match user groups to device groups by keyword
    # Admins (Lab Admins, Lab Managers) get no device group permissions (admin role sees all)
    # Tier 1 user groups map to their matching device group
    print("\n--- Device Group Permissions ---")
    keyword_map = {
        "Alpha": sclab_id,
        "Bravo": plano_id,
        "Charlie": almere_id,
    }
    for i, (dg_id, dg_name) in enumerate(zip(dg_ids, dg_names)):
        matched_user_group_ids = []
        for keyword, ug_id in keyword_map.items():
            if keyword.lower() in dg_name.lower():
                matched_user_group_ids.append(ug_id)
        if matched_user_group_ids:
            print(f"  {dg_name}: assigning {len(matched_user_group_ids)} user group(s)")
            bulk_add_permissions_to_device_group(client, dg_id, matched_user_group_ids)
        else:
            print(f"  {dg_name}: no matching user groups")

    # Dedicated ACL test users + Santa Clara device group (scoped-visibility demo)
    seed_acl_test_fixtures(client)

    # FRR live-config demo (opt-in): real netmiko driver + the two slice1 lab routers.
    # Off by default so the standard seed population is unchanged; set SEED_FRR=1 to add it.
    if seed_frr:
        seed_frr_demo(client)

    # NOS test lab (opt-in): real srl_l2 + frr_l3 drivers plus the two
    # infra/nos-test lab nodes. Off by default so the standard seed population
    # is unchanged; set SEED_NOS=1 to add it (docs/NOS_LAB.md).
    if seed_nos:
        seed_nos_lab(client)

    total_dut_ports = sum(
        TEMPLATE_PORT_COUNTS.get(d["template"], DEFAULT_PORT_COUNT) for d in DEVICES
    )
    total_l1_ports = NUM_L1_SWITCHES * L1_PORTS_TOTAL
    total_l2_ports = NUM_L2_SWITCHES * L2_PORTS_TOTAL
    print(
        f"\nDone. {NUM_ADMINS + NUM_USERS} users, 4 drivers, "
        f"{len(DEVICE_TEMPLATES) + 4} templates, "
        f"{TOTAL_DEVICES} DUT devices + {NUM_L1_SWITCHES} L1 + {NUM_L2_SWITCHES} L2 switches "
        f"+ {len(isolated_device_ids)} isolated demo devices, "
        f"{total_dut_ports} DUT ports + {total_l1_ports} L1 ports + {total_l2_ports} L2 ports, "
        f"{num_connections} connections, "
        f"3 device groups, 5 user groups, 3 device group permissions, "
        f"{num_valid_topos} valid + {num_invalid_topos} invalid topologies."
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m seedtools",
        description=(
            "Seed a running HERD stack. Every subcommand is re-runnable and skips "
            "resources that already exist."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser(
        "full",
        help=(
            "the standard demo population; SEED_FRR=1 and SEED_NOS=1 layer the "
            "FRR demo and the NOS lab on top"
        ),
    )
    subparsers.add_parser(
        "acl",
        help="only the Santa Clara ACL fixtures (scoped user, unscoped admin, one group)",
    )
    frr = subparsers.add_parser(
        "frr",
        help="only the FRR live-config demo (the real netmiko driver plus the two slice1 routers)",
    )
    frr.add_argument(
        "--full",
        action="store_true",
        help="run the whole population with the FRR demo layered on",
    )
    nos = subparsers.add_parser(
        "nos",
        help="only the NOS test lab (the real srl_l2 and frr_l3 drivers and the two lab nodes)",
    )
    nos.add_argument(
        "--full",
        action="store_true",
        help="run the whole population with the NOS lab layered on",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    command = args.command
    layered = getattr(args, "full", False)

    # SEED_FRR / SEED_NOS keep their names and their meaning on `full`; the frr
    # and nos subcommands force their own demo on regardless.
    env_frr = os.environ.get("SEED_FRR") == "1"
    env_nos = os.environ.get("SEED_NOS") == "1"

    if command == "full":
        extras = [
            label
            for label, on in (("FRR live-config demo", env_frr), ("NOS test lab", env_nos))
            if on
        ]
        suffix = f" plus the {' and the '.join(extras)}" if extras else ""
        print(
            f"Seeding {BASE} (users, devices, switches, cabling, groups, isolated demo "
            f"devices, demo topologies){suffix} as {EMAIL}"
        )
        run_full(connect(), seed_frr=env_frr, seed_nos=env_nos)
        return 0

    if command == "acl":
        print(f"Seeding {BASE} with the Santa Clara ACL fixtures as {EMAIL}")
        seed_acl_test_fixtures(connect())
        return 0

    if command == "frr":
        if layered:
            print(
                f"Seeding {BASE} with the full demo population plus the FRR "
                f"live-config demo as {EMAIL}"
            )
            run_full(connect(), seed_frr=True, seed_nos=env_nos)
        else:
            print(f"Seeding {BASE} with the FRR live-config demo as {EMAIL}")
            seed_frr_demo(connect())
        return 0

    if command == "nos":
        if layered:
            print(f"Seeding {BASE} with the full demo population plus the NOS test lab as {EMAIL}")
            run_full(connect(), seed_frr=env_frr, seed_nos=True)
        else:
            print(f"Seeding {BASE} with the NOS test lab as {EMAIL}")
            seed_nos_lab(connect())
        return 0

    raise AssertionError(f"unhandled subcommand {command!r}")  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
