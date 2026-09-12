"""The NOS test lab (infra/nos-test, docs/NOS_LAB.md): the real srl_l2 and
frr_l3 driver packages plus the two lab nodes and their groundwork.
"""

import os

import httpx

from .catalog import PORT_SECTIONS, SECTIONS
from .client import BASE, REPO_ROOT
from .drivers import _make_driver_zip_from_dir, get_or_create_driver
from .inventory import (
    create_ports,
    get_or_create_device,
    get_or_create_management_device,
    get_or_create_template,
)

# --- NOS test lab (infra/nos-test, docs/NOS_LAB.md): real drivers against two
# real, license-free network operating systems (ADR 0010 phase 3a). ---

NOS_LAB_SRL_CONTAINER = "nos-test-srl"
NOS_LAB_FRR_CONTAINER = "nos-test-frr"
NOS_LAB_SRL_LOGIN = os.environ.get("SEED_NOS_SRL_LOGIN", "admin")
NOS_LAB_SRL_PASSWORD = os.environ.get("SEED_NOS_SRL_PASSWORD", "NokiaSrl1!")
NOS_LAB_FRR_LOGIN = os.environ.get("SEED_NOS_FRR_LOGIN", "netadmin")
NOS_LAB_FRR_PASSWORD = os.environ.get("SEED_NOS_FRR_PASSWORD", "netadmin")

SRL_L2_DRIVER_DIR = os.path.join(REPO_ROOT, "drivers", "srl_l2")
FRR_L3_DRIVER_DIR = os.path.join(REPO_ROOT, "drivers", "frr_l3")


def seed_nos_lab(client: httpx.Client) -> None:
    """Register the real srl_l2 and frr_l3 drivers plus the two NOS test lab
    nodes (infra/nos-test, docs/NOS_LAB.md), and lay down enough DUT/port/
    cabling groundwork for a later phase to derive an L2 VLAN membership from
    recorded L1 hops (ADR 0009).

    Idempotent and opt-in (gated by SEED_NOS=1 in main), mirroring
    seed_frr_demo's shape. Unlike the FRR live-config demo (a Management
    driver, generic "configure" apply job), srl_l2 and frr_l3 implement the
    narrower Layer 2 Switch and Layer 3 Switch driver contracts: HERD only
    ever drives them through a reservation fork's wiring/routing intent (ADR
    0009 / ADR 0014), never a generic config-apply job. This function seeds
    the inventory-side groundwork only, devices, ports, cabling; it
    deliberately creates no topology or reservation. See
    tests/nos_lab/test_frr_l3_via_stack_live.py for the reservation-driven,
    end-to-end proof that exercises this.

    The two lab nodes are reachable from the execution service by CONTAINER
    NAME (nos-test-srl, nos-test-frr) once attached to the stack's Docker
    network (`make nos-attach`) over Docker DNS; container IPs are not stable
    across a recreate, container names are. So field_data.ip here is the
    container name, not an IP address.
    """
    print("\n--- NOS test lab (real SR Linux + FRR) ---")
    srl_zip = _make_driver_zip_from_dir(SRL_L2_DRIVER_DIR)
    frr_l3_zip = _make_driver_zip_from_dir(FRR_L3_DRIVER_DIR)
    if srl_zip is None or frr_l3_zip is None:
        return  # warning already printed; nothing to seed without both packages

    srl_driver_id = get_or_create_driver(
        client,
        "Nokia SR Linux L2 Switch Driver",
        "Layer 2 Switch",
        zip_bytes=srl_zip,
        replace_if_exists=True,
    )
    frr_l3_driver_id = get_or_create_driver(
        client,
        "FRRouting L3 Switch Driver",
        "Layer 3 Switch",
        zip_bytes=frr_l3_zip,
        replace_if_exists=True,
    )
    # A DUT's own driver is never invoked: execution only ever drives the
    # L1/L2/L3 switch endpoint of a recorded hop, never a leaf DUT. A dummy
    # zip is enough; the "Management" connection_type is arbitrary here.
    dut_driver_id = get_or_create_driver(client, "NOS Lab DUT Placeholder Driver", "Management")

    srl_template_id = get_or_create_template(
        client,
        "NOS Lab SR Linux L2 Switch",
        "device",
        "Nokia SR Linux, driven over SSH via netmiko (drivers/srl_l2). "
        "The real infra/nos-test lab node (docs/NOS_LAB.md).",
        SECTIONS,
        driver_id=srl_driver_id,
        vendor="Nokia",
        model="SR Linux",
    )
    frr_l3_template_id = get_or_create_template(
        client,
        "NOS Lab FRR L3 Switch",
        "device",
        "FRRouting router driven over SSH via vtysh, Layer 3 Switch contract "
        "(drivers/frr_l3). The real infra/nos-test lab node (docs/NOS_LAB.md).",
        SECTIONS,
        driver_id=frr_l3_driver_id,
        vendor="FRRouting",
        model="FRR (nos-test lab)",
    )
    dut_template_id = get_or_create_template(
        client,
        "NOS Lab DUT",
        "device",
        "Placeholder device-under-test cabled to the NOS test lab for L1 hop "
        "groundwork (ADR 0009); its own driver is never invoked.",
        SECTIONS,
        driver_id=dut_driver_id,
        vendor="Generic",
        model="NOS Lab DUT",
    )
    port_template_id = get_or_create_template(
        client, "1Gb Copper", "port", "1Gb copper Ethernet port", PORT_SECTIONS
    )

    srl_id = get_or_create_management_device(
        client,
        "nos-lab-srl",
        srl_template_id,
        NOS_LAB_SRL_CONTAINER,
        NOS_LAB_SRL_LOGIN,
        NOS_LAB_SRL_PASSWORD,
    )
    print(f"  SR Linux device: nos-lab-srl, container {NOS_LAB_SRL_CONTAINER} ({srl_id})")

    frr_l3_id = get_or_create_management_device(
        client,
        "nos-lab-frr",
        frr_l3_template_id,
        NOS_LAB_FRR_CONTAINER,
        NOS_LAB_FRR_LOGIN,
        NOS_LAB_FRR_PASSWORD,
    )
    print(f"  FRR device: nos-lab-frr, container {NOS_LAB_FRR_CONTAINER} ({frr_l3_id})")

    dut_ids: list[str] = []
    for i in (1, 2):
        dut_id = get_or_create_device(client, f"nos-lab-dut-{i}", dut_template_id, f"192.0.2.{i}")
        dut_ids.append(dut_id)
        print(f"  DUT device: nos-lab-dut-{i} ({dut_id})")

    # ethernet-1/1 and ethernet-1/2 are the two SR Linux interfaces the
    # checked-in lab baseline (infra/nos-test/srl/baseline.cli) already
    # enables and vlan-tags, so they are the two ports a later phase can
    # actually drive create_vlan/add_to_vlan against.
    create_ports(client, srl_id, port_template_id, count=2, prefix="ethernet-1/")
    create_ports(client, dut_ids[0], port_template_id, count=1, prefix="eth")
    create_ports(client, dut_ids[1], port_template_id, count=1, prefix="eth")
    create_ports(client, frr_l3_id, port_template_id, count=1, prefix="eth")

    _seed_nos_lab_connection(client, dut_ids[0], srl_id, "ethernet-1/1")
    _seed_nos_lab_connection(client, dut_ids[1], srl_id, "ethernet-1/2")


def _seed_nos_lab_connection(
    client: httpx.Client, dut_id: str, switch_id: str, switch_port: str
) -> None:
    """Cable dut_id:eth1 to switch_id:switch_port. Idempotent: skips if the
    DUT already has any connection."""
    existing = client.get(f"{BASE}/cabling/connections", params={"device_id": dut_id, "limit": 1})
    if existing.status_code == 200:
        data = existing.json()
        if data.get("total", len(data.get("items", []))) > 0:
            print(f"  Exists NOS lab cabling: {dut_id} already connected")
            return
    resp = client.post(
        f"{BASE}/cabling/connections",
        json={
            "device_a_id": dut_id,
            "port_a": "eth1",
            "device_b_id": switch_id,
            "port_b": switch_port,
        },
    )
    if resp.status_code == 201:
        print(f"  Created NOS lab cabling: {dut_id}:eth1 -- {switch_id}:{switch_port}")
    else:
        print(f"  WARNING: NOS lab cabling not created ({resp.status_code}): {resp.text}")
