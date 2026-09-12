"""Inventory writes: templates, devices, ports, and the DUT-template picker."""

import sys

import httpx

from .catalog import L1_PORTS_PER_SLOT, L1_SLOTS
from .client import BASE, fetch_all_items


def get_or_create_template(
    client: httpx.Client,
    name: str,
    template_type: str,
    description: str,
    sections: list,
    driver_id: str | None = None,
    exclusive: bool | None = None,
    vendor: str | None = None,
    model: str | None = None,
    part_number: str | None = None,
    poll_interval_seconds: int | None = None,
) -> str:
    body: dict = {
        "name": name,
        "template_type": template_type,
        "description": description,
        "sections": sections,
    }
    if driver_id is not None:
        body["driver_id"] = driver_id
    if exclusive is not None:
        body["exclusive"] = exclusive
    if vendor is not None:
        body["vendor"] = vendor
    if model is not None:
        body["model"] = model
    if part_number is not None:
        body["part_number"] = part_number
    if poll_interval_seconds is not None:
        body["poll_interval_seconds"] = poll_interval_seconds
    resp = client.post(f"{BASE}/inventory/templates", json=body)
    if resp.status_code == 201:
        tid = resp.json()["id"]
        print(f"  Created template: {name} ({tid})")
        return tid

    # Already exists; look it up
    listing = client.get(
        f"{BASE}/inventory/templates",
        params={"template_type": template_type, "limit": 500},
    )
    for t in listing.json()["items"]:
        if t["name"] == name:
            print(f"  Exists template: {name} ({t['id']})")
            return t["id"]

    print(f"  Failed to create or find template {name}: {resp.text}")
    sys.exit(1)


def get_or_create_device(
    client: httpx.Client,
    name: str,
    template_id: str,
    ip: str,
    poll_interval_seconds: int | None = None,
) -> str:
    field_data = {"ip": ip, "login": "admin", "password": "admin123"}
    assert set(field_data) == set(SEED_DEVICE_FIELD_KEYS), (
        f"field_data keys {sorted(field_data)} != SEED_DEVICE_FIELD_KEYS "
        f"{sorted(SEED_DEVICE_FIELD_KEYS)}; keep the two in step"
    )
    body: dict = {
        "name": name,
        "template_id": template_id,
        "topology_type": "PHYSICAL",
        "field_data": field_data,
    }
    if poll_interval_seconds is not None:
        body["poll_interval_seconds"] = poll_interval_seconds
    resp = client.post(
        f"{BASE}/inventory/devices",
        json=body,
    )
    if resp.status_code == 201:
        did = resp.json()["id"]
        return did

    if resp.status_code == 409:
        # Already exists; look it up
        all_devices = fetch_all_items(client, f"{BASE}/inventory/devices")
        for d in all_devices:
            if d["name"] == name:
                return d["id"]

    print(f"  Failed to create or find device {name}: {resp.text}")
    sys.exit(1)


def create_ports(
    client: httpx.Client,
    device_id: str,
    port_template_id: str,
    count: int = 32,
    prefix: str = "eth",
) -> None:
    existing = client.get(f"{BASE}/inventory/devices/{device_id}/ports")
    if existing.status_code == 200 and len(existing.json()) > 0:
        return

    # Bulk create endpoint accepts max 200 per call
    created = 0
    while created < count:
        batch = min(200, count - created)
        client.post(
            f"{BASE}/inventory/devices/{device_id}/ports/bulk",
            json={
                "name_prefix": prefix,
                "starting_index": created + 1,
                "instances": batch,
                "template_id": port_template_id,
                "field_data": {},
            },
        )
        created += batch


def get_or_create_management_device(
    client: httpx.Client,
    name: str,
    template_id: str,
    ip: str,
    login: str,
    password: str,
) -> str:
    """Create (or find) a Management device with explicit SSH credentials.

    Unlike get_or_create_device, which hardcodes generic admin creds for the bulk
    DUT population, this threads real per-device creds into field_data so the
    execution sandbox hands the driver working HERD_login / HERD_password.
    """
    field_data = {"ip": ip, "login": login, "password": password}
    body = {
        "name": name,
        "template_id": template_id,
        "topology_type": "PHYSICAL",
        "field_data": field_data,
    }
    resp = client.post(f"{BASE}/inventory/devices", json=body)
    if resp.status_code == 201:
        return resp.json()["id"]
    if resp.status_code == 409:
        for d in fetch_all_items(client, f"{BASE}/inventory/devices"):
            if d["name"] == name:
                return d["id"]
    print(f"  Failed to create or find device {name}: {resp.text}")
    sys.exit(1)


def create_l1_switch_ports(
    client: httpx.Client,
    device_id: str,
    port_template_id: str,
) -> None:
    """Create 256 ports on an L1 switch using backplane naming: 0/slot/port."""
    existing = client.get(f"{BASE}/inventory/devices/{device_id}/ports")
    if existing.status_code == 200 and len(existing.json()) > 0:
        return

    for slot in range(L1_SLOTS):
        client.post(
            f"{BASE}/inventory/devices/{device_id}/ports/bulk",
            json={
                "name_prefix": f"0/{slot}/",
                "starting_index": 1,
                "instances": L1_PORTS_PER_SLOT,
                "template_id": port_template_id,
                "field_data": {},
            },
        )


# The field keys get_or_create_device always sends in field_data. A template that does
# not declare all of them rejects the create with "Unknown fields: ...", so any helper
# that CHOOSES a template for those devices has to filter on this set.
SEED_DEVICE_FIELD_KEYS = ("ip", "login", "password")


def template_field_keys(template: dict) -> set[str]:
    """The field keys a device template declares, flattened across its sections."""
    return {
        field["key"]
        for section in (template.get("sections") or [])
        for field in (section.get("fields") or [])
        if field.get("key") is not None
    }


def template_declares_seed_fields(template: dict) -> bool:
    """Whether `template` accepts every field key get_or_create_device sends."""
    return set(SEED_DEVICE_FIELD_KEYS).issubset(template_field_keys(template))


def pick_dut_template(client: httpx.Client) -> str | None:
    """Pick a device template backed by a Management-connection driver (a DUT).

    The non-admin device list is dut_only: it shows only devices whose driver
    connection_type is Management. A switch-backed template would be filtered out
    of a scoped user's view, so the Santa Clara demo devices must be DUTs.

    Only templates that declare every key in SEED_DEVICE_FIELD_KEYS are eligible,
    because get_or_create_device always sends those and inventory rejects a create
    carrying fields the template does not declare ("Unknown fields: ip, login,
    password"). Without that filter the pick is order-dependent and a
    Management-backed template that declares something else entirely (the
    integration suite seeds `int-seed-template-*` rows whose only field is `model`)
    can win and break seeding on any database those tests have touched. Falls back
    to the first usable template; returns None when no template is usable (issue
    #781, the worked example is #775), never a template guaranteed to fail
    validation. The caller, seed_acl_test_fixtures, handles None by skipping.
    """
    drivers = client.get(f"{BASE}/inventory/drivers", params={"limit": 500}).json().get("items", [])
    mgmt_driver_ids = {d["id"] for d in drivers if d.get("connection_type") == "Management"}
    listing = client.get(
        f"{BASE}/inventory/templates",
        params={"template_type": "device", "limit": 500},
    )
    items = listing.json().get("items", [])
    usable = [t for t in items if template_declares_seed_fields(t)]
    for t in usable:
        if t.get("driver_id") in mgmt_driver_ids:
            return t["id"]
    return usable[0]["id"] if usable else None
