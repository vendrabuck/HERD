"""The FRR live-config demo: the real netmiko driver plus the two slice1 lab
routers, their cabling, a demo topology, and an ACTIVE reservation.
"""

import os
import time
from datetime import datetime, timedelta, timezone

import httpx

from .catalog import SECTIONS
from .client import BASE, fetch_all_items
from .drivers import _make_frr_driver_zip, get_or_create_driver
from .inventory import get_or_create_management_device, get_or_create_template
from .topologies import (
    _chain_edges,
    build_canvas,
    get_or_create_topology,
    list_existing_topology_names,
)

# The live FRR routers in the network-simulator slice1 lab (topologies/slice1.clab.yml).
# r1/r2 run frrouting/frr with an SSH-to-vtysh login (netadmin/demo123, shell /usr/bin/vtysh)
# on the 10.99.0.0/24 management plane. These are the real targets the FRR driver configures
# in the Thursday live-config demo. demo123 is a throwaway lab password, not a secret.
FRR_LAB_DEVICES = [
    {"name": "frr-r1", "ip": "10.99.0.11"},
    {"name": "frr-r2", "ip": "10.99.0.12"},
]
FRR_LAB_LOGIN = os.environ.get("SEED_FRR_LOGIN", "netadmin")
FRR_LAB_PASSWORD = os.environ.get("SEED_FRR_PASSWORD", "demo123")


def seed_frr_demo(client: httpx.Client) -> None:
    """Register the real FRR netmiko driver and the two live lab routers.

    Idempotent and opt-in (gated by SEED_FRR=1 in main) so the heavy default seed
    is unchanged. This is the spine of the live-config demo: HERD drives a real FRR
    router over SSH via vtysh. The devices point at the network-simulator slice1 lab
    (10.99.0.11/.12); they are reachable only when that lab is up on the 10.99.0.0/24
    subnet, which is expected (the driver's status() reports unreachable otherwise).
    """
    print("\n--- FRR live-config demo ---")
    zip_bytes = _make_frr_driver_zip()
    if zip_bytes is None:
        return  # warning already printed; nothing to seed without the package

    frr_driver_id = get_or_create_driver(
        client,
        "FRRouting Management Driver",
        "Management",
        zip_bytes=zip_bytes,
        # The driver package evolves between demo prep runs (e.g. adding
        # config_schema()), but get_or_create_driver is idempotent by NAME, so a
        # re-seed against a DB that already has the driver would keep the stale
        # package. Force a file replace so the latest driver.py (and its new
        # SHA, which makes the execution service re-extract config_schema())
        # actually reaches inventory.
        replace_if_exists=True,
    )
    frr_template_id = get_or_create_template(
        client,
        "FRRouting Router",
        "device",
        "FRRouting router driven over SSH via vtysh (netmiko). Live-config demo target.",
        SECTIONS,
        driver_id=frr_driver_id,
        vendor="FRRouting",
        model="FRR (slice1 lab)",
    )
    device_ids: list[str] = []
    for dev in FRR_LAB_DEVICES:
        device_id = get_or_create_management_device(
            client,
            dev["name"],
            frr_template_id,
            dev["ip"],
            FRR_LAB_LOGIN,
            FRR_LAB_PASSWORD,
        )
        print(f"  FRR device: {dev['name']} -> {dev['ip']} ({device_id})")
        device_ids.append(device_id)

    # The headline flow runs the AI reservation assistant + the editable topology
    # fork, both of which need an ACTIVE reservation and a topology canvas. Seed
    # the physical cabling first: a reservation that binds a topology rejects any
    # canvas edge with no backing Connection path (no_path), so r1 and r2 must be
    # physically connected for the topology to validate and the fork to copy a
    # real canvas.
    seed_frr_connection(client, device_ids)
    topology_id = seed_frr_topology(client, device_ids)
    ensure_frr_reservation(client, device_ids, topology_id)


def seed_frr_connection(client: httpx.Client, device_ids: list[str]) -> None:
    """Cable frr-r1:eth1 to frr-r2:eth1 so the demo topology has a real path.

    The reservation's topology validation walks the physical Connection graph;
    without this connection the canvas edge r1 -- r2 is rejected as no_path.
    Idempotent: skips if r1 already has any connection.
    """
    if len(device_ids) < 2:
        return
    r1, r2 = device_ids[0], device_ids[1]
    existing = client.get(f"{BASE}/cabling/connections", params={"device_id": r1, "limit": 1})
    if existing.status_code == 200:
        data = existing.json()
        if data.get("total", len(data.get("items", []))) > 0:
            print("  Exists FRR cabling: r1 already connected")
            return
    resp = client.post(
        f"{BASE}/cabling/connections",
        json={"device_a_id": r1, "port_a": "eth1", "device_b_id": r2, "port_b": "eth1"},
    )
    if resp.status_code == 201:
        print("  Created FRR cabling: frr-r1:eth1 -- frr-r2:eth1")
    else:
        print(f"  WARNING: FRR cabling not created ({resp.status_code}): {resp.text}")


def seed_frr_topology(client: httpx.Client, device_ids: list[str]) -> str | None:
    """Create the FRR demo topology: frr-r1 -- frr-r2 as a 2-node L3 chain.

    Reuses build_canvas / get_or_create_topology (the same path the bulk topology
    seed uses) so the seeded canvas is indistinguishable from a hand-built one.
    Idempotent by name; returns the topology id, or None if it already existed or
    creation failed (the demo still works without a canvas, just less pretty).
    """
    if len(device_ids) < 2:
        print("  (skipping FRR topology: need both routers)")
        return None
    # Fetch the full device records so the canvas nodes carry the same fields the
    # editor renders (name, status, template_name, ...), not just bare ids.
    lookup: dict[str, dict] = {}
    for did in device_ids:
        resp = client.get(f"{BASE}/inventory/devices/{did}")
        if resp.status_code == 200:
            lookup[did] = resp.json()
    canvas = build_canvas(
        list(device_ids),
        _chain_edges(len(device_ids), "L3"),
        device_lookup=lookup,
    )
    name = "FRR Live-Config Demo"
    description = "frr-r1 and frr-r2 on the slice1 lab; the live-config demo stage."
    tid = get_or_create_topology(
        client,
        name,
        canvas,
        list_existing_topology_names(client),
        description=description,
    )
    if tid is not None:
        return tid

    # Already existed: get_or_create_topology returns None on the exists path, but
    # the reservation needs the id to BIND the topology (an unbound reservation
    # gets no editable fork). Resolve it by name and refresh its canvas so a
    # re-seed keeps the r1 -- r2 link current.
    for t in fetch_all_items(client, f"{BASE}/cabling/topologies"):
        if t["name"] == name:
            client.put(
                f"{BASE}/cabling/topologies/{t['id']}",
                json={"canvas_data": canvas, "description": description},
            )
            print(f"  Refreshed topology canvas: {name} ({t['id']})")
            return t["id"]
    return None


def ensure_frr_reservation(
    client: httpx.Client,
    device_ids: list[str],
    topology_id: str | None,
) -> str | None:
    """Create an ACTIVE reservation over the FRR routers for the demo.

    The FRR template is exclusive, so a fresh reservation is born
    PENDING_PROVISION and only flips to ACTIVE once the execution service
    provisions it over NATS (which also creates the editable fork the assistant
    needs). We create it and then POLL until ACTIVE, so the seed mirrors the real
    demo path instead of faking the end state. Idempotent: if an ACTIVE/pending
    reservation already covers these devices, reuse it.

    Returns the reservation id, or None on failure (logged, non-fatal).
    """
    if not device_ids:
        return None

    # Idempotency: reuse an existing live reservation over r1 ONLY if it already
    # binds our topology (an unbound or differently-bound reservation gets no
    # editable fork for this topology). If a live one covers r1 but is not bound
    # to our topology, cancel it so we can create a correctly-bound replacement;
    # otherwise a stale unbound reservation would block the create on conflict.
    live = {"ACTIVE", "PENDING", "PENDING_PROVISION"}
    existing = client.get(f"{BASE}/reservations/", params={"limit": 200})
    if existing.status_code == 200:
        for r in existing.json().get("items", []):
            covers_r1 = device_ids[0] in [str(d) for d in r.get("device_ids", [])]
            if r.get("status") not in live or not covers_r1:
                continue
            if topology_id and str(r.get("topology_id")) == str(topology_id):
                print(f"  Exists reservation: {r['id']} (bound, status {r.get('status')})")
                return _wait_for_active(client, r["id"])
            if not topology_id:
                print(f"  Exists reservation: {r['id']} (status {r.get('status')})")
                return _wait_for_active(client, r["id"])
            # Live but not bound to our topology: cancel and replace.
            cancel = client.delete(f"{BASE}/reservations/{r['id']}")
            print(
                f"  Cancelled stale reservation {r['id']} "
                f"(was unbound/mismatched): HTTP {cancel.status_code}"
            )

    now = datetime.now(timezone.utc)
    body = {
        "device_ids": device_ids,
        "purpose": "FRR live-config demo: AI proposes a route, applied over SSH.",
        "start_time": now.isoformat(),
        "end_time": (now + timedelta(hours=8)).isoformat(),
    }
    if topology_id:
        body["topology_id"] = topology_id
    resp = client.post(f"{BASE}/reservations/", json=body)
    if resp.status_code != 201:
        print(f"  WARNING: reservation not created ({resp.status_code}): {resp.text}")
        return None
    res = resp.json()
    print(f"  Created reservation: {res['id']} (status {res.get('status')})")
    return _wait_for_active(client, res["id"])


def _wait_for_active(client: httpx.Client, reservation_id: str, timeout_s: int = 30) -> str:
    """Poll a reservation until it reaches ACTIVE, or give up after timeout_s.

    Born-ACTIVE reservations return immediately. A PENDING_PROVISION reservation
    flips to ACTIVE once execution provisions it over NATS; we poll rather than
    assume so the seed reflects reality. A terminal FAILED/CANCELLED stops the
    wait early. Returns the reservation id regardless so the caller can report it.
    """
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        r = client.get(f"{BASE}/reservations/{reservation_id}")
        if r.status_code == 200:
            last = r.json().get("status")
            if last == "ACTIVE":
                print(f"  Reservation {reservation_id} is ACTIVE.")
                return reservation_id
            if last in ("FAILED", "CANCELLED"):
                print(f"  WARNING: reservation {reservation_id} ended {last}, not ACTIVE.")
                return reservation_id
        time.sleep(1.0)
    print(f"  WARNING: reservation {reservation_id} still {last} after {timeout_s}s (not ACTIVE).")
    return reservation_id
