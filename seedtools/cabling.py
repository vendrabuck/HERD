"""Physical cabling: the L1 and L2 connection fabrics."""

import httpx

from .catalog import (
    L1_PORTS_PER_SLOT,
    L2_DUT_PORTS_MAX,
    L2_PORTS_PER_CLIENT,
    L2_PORTS_PER_PA,
)
from .client import BASE


def create_connections(
    client: httpx.Client,
    pa_device_ids: list[str],
    client_device_ids: list[str],
    edge_switch_ids: list[str],
    hub_switch_ids: list[str],
) -> int:
    """Create all cabling connections: DUT-to-edge, edge-to-hub, hub-to-hub.

    PA devices get up to 5 connections (eth1-eth5), client devices get 2 (eth1-eth2).
    Edge switches fill until slots 0-6 are exhausted; remaining DUTs are skipped.

    Returns total connections created.
    """
    PORTS_PER_PA = 6
    PORTS_PER_CLIENT = 2

    # Re-runnability guard: check if first edge switch already has connections
    if edge_switch_ids:
        resp = client.get(
            f"{BASE}/cabling/connections",
            params={"device_id": edge_switch_ids[0], "limit": 1},
        )
        if resp.status_code == 200:
            data = resp.json()
            total = data.get("total", len(data.get("items", [])))
            if total > 0:
                print("  Connections already exist, skipping cabling")
                return 0

    created = 0

    # DUT-to-edge: round-robin DUTs across edge switches
    # Each edge switch uses slots 0-6 (224 ports) for DUT connections
    dut_ports_per_switch = 7 * L1_PORTS_PER_SLOT  # 224
    edge_port_counters = [0] * len(edge_switch_ids)

    total_pa = len(pa_device_ids)
    print(
        f"  Creating PA-to-edge connections ({total_pa} PA devices, {PORTS_PER_PA} ports each)..."
    )
    for i, dut_id in enumerate(pa_device_ids):
        edge_idx = i % len(edge_switch_ids)
        for port_idx in range(PORTS_PER_PA):
            port_num = edge_port_counters[edge_idx]
            if port_num >= dut_ports_per_switch:
                break
            slot = port_num // L1_PORTS_PER_SLOT
            port = (port_num % L1_PORTS_PER_SLOT) + 1
            edge_port_name = f"0/{slot}/{port}"
            resp = client.post(
                f"{BASE}/cabling/connections",
                json={
                    "device_a_id": dut_id,
                    "port_a": f"eth{port_idx + 1}",
                    "device_b_id": edge_switch_ids[edge_idx],
                    "port_b": edge_port_name,
                    "connection_type": "ethernet",
                },
            )
            if resp.status_code == 201:
                created += 1
            edge_port_counters[edge_idx] += 1

        done = i + 1
        if done % 500 == 0 or done == total_pa:
            print(f"    PA connections: {done}/{total_pa} devices (created={created})")

    total_clients = len(client_device_ids)
    pa_created = created
    print(
        f"  Creating client-to-edge connections ({total_clients} client devices, "
        f"{PORTS_PER_CLIENT} ports each)..."
    )
    for i, dut_id in enumerate(client_device_ids):
        edge_idx = (total_pa + i) % len(edge_switch_ids)
        for port_idx in range(PORTS_PER_CLIENT):
            port_num = edge_port_counters[edge_idx]
            if port_num >= dut_ports_per_switch:
                break
            slot = port_num // L1_PORTS_PER_SLOT
            port = (port_num % L1_PORTS_PER_SLOT) + 1
            edge_port_name = f"0/{slot}/{port}"
            resp = client.post(
                f"{BASE}/cabling/connections",
                json={
                    "device_a_id": dut_id,
                    "port_a": f"eth{port_idx + 1}",
                    "device_b_id": edge_switch_ids[edge_idx],
                    "port_b": edge_port_name,
                    "connection_type": "ethernet",
                },
            )
            if resp.status_code == 201:
                created += 1
            edge_port_counters[edge_idx] += 1

        done = i + 1
        if done % 200 == 0 or done == total_clients:
            print(
                f"    Client connections: {done}/{total_clients} devices "
                f"(created={created - pa_created})"
            )

    # Edge-to-hub: each edge connects to both hubs via slot 7
    print(f"  Creating edge-to-hub connections ({len(edge_switch_ids)} x {len(hub_switch_ids)})...")
    hub_port_counters = [0] * len(hub_switch_ids)
    for edge_id in edge_switch_ids:
        for hub_idx, hub_id in enumerate(hub_switch_ids):
            hub_port = hub_port_counters[hub_idx] + 1
            hub_slot = (hub_port - 1) // L1_PORTS_PER_SLOT
            hub_port_in_slot = ((hub_port - 1) % L1_PORTS_PER_SLOT) + 1
            resp = client.post(
                f"{BASE}/cabling/connections",
                json={
                    "device_a_id": edge_id,
                    "port_a": f"0/7/{hub_idx + 1}",
                    "device_b_id": hub_id,
                    "port_b": f"0/{hub_slot}/{hub_port_in_slot}",
                    "connection_type": "ethernet",
                },
            )
            if resp.status_code == 201:
                created += 1
            hub_port_counters[hub_idx] += 1

    # Hub-to-hub: one connection between the two hubs
    if len(hub_switch_ids) == 2:
        print("  Creating hub-to-hub connection...")
        resp = client.post(
            f"{BASE}/cabling/connections",
            json={
                "device_a_id": hub_switch_ids[0],
                "port_a": "0/7/1",
                "device_b_id": hub_switch_ids[1],
                "port_b": "0/7/1",
                "connection_type": "ethernet",
            },
        )
        if resp.status_code == 201:
            created += 1

    return created


def create_l2_connections(
    client: httpx.Client,
    pa_device_ids: list[str],
    client_device_ids: list[str],
    l2_switch_ids: list[str],
    l2_hub_switch_ids: list[str],
) -> int:
    """Create L2 cabling connections: DUT-to-edge, edge-to-hub, hub-to-hub.

    PA devices use eth6-eth7 (2 ports per device), clients use eth3 (1 port).
    DUTs are round-robin distributed across L2 switches within their lab.
    Edge switches use eth47-eth48 as uplinks to the two hub switches.
    Hub switches connect to each other for full L2 star connectivity.

    Returns total connections created.
    """
    if not l2_switch_ids:
        return 0

    # Re-runnability guard
    resp = client.get(
        f"{BASE}/cabling/connections",
        params={"device_id": l2_switch_ids[0], "limit": 1},
    )
    if resp.status_code == 200:
        data = resp.json()
        total = data.get("total", len(data.get("items", [])))
        if total > 0:
            print("  L2 connections already exist, skipping")
            return 0

    created = 0
    l2_port_counters = [0] * len(l2_switch_ids)

    # PA devices: eth6, eth7 to L2 switches
    total_pa = len(pa_device_ids)
    print(
        f"  Creating PA-to-L2 connections ({total_pa} PA devices, {L2_PORTS_PER_PA} ports each)..."
    )
    for i, dut_id in enumerate(pa_device_ids):
        l2_idx = i % len(l2_switch_ids)
        for port_idx in range(L2_PORTS_PER_PA):
            l2_port = l2_port_counters[l2_idx] + 1
            if l2_port > L2_DUT_PORTS_MAX:
                break
            resp = client.post(
                f"{BASE}/cabling/connections",
                json={
                    "device_a_id": dut_id,
                    "port_a": f"eth{6 + port_idx}",
                    "device_b_id": l2_switch_ids[l2_idx],
                    "port_b": f"eth{l2_port}",
                    "connection_type": "ethernet",
                },
            )
            if resp.status_code == 201:
                created += 1
            l2_port_counters[l2_idx] += 1

        done = i + 1
        if done % 500 == 0 or done == total_pa:
            print(f"    PA L2 connections: {done}/{total_pa} devices (created={created})")

    # Client devices: eth3 to L2 switches
    total_clients = len(client_device_ids)
    pa_l2_created = created
    print(
        f"  Creating client-to-L2 connections ({total_clients} client devices, "
        f"{L2_PORTS_PER_CLIENT} port each)..."
    )
    for i, dut_id in enumerate(client_device_ids):
        l2_idx = (total_pa + i) % len(l2_switch_ids)
        for port_idx in range(L2_PORTS_PER_CLIENT):
            l2_port = l2_port_counters[l2_idx] + 1
            if l2_port > L2_DUT_PORTS_MAX:
                break
            resp = client.post(
                f"{BASE}/cabling/connections",
                json={
                    "device_a_id": dut_id,
                    "port_a": f"eth{3 + port_idx}",
                    "device_b_id": l2_switch_ids[l2_idx],
                    "port_b": f"eth{l2_port}",
                    "connection_type": "ethernet",
                },
            )
            if resp.status_code == 201:
                created += 1
            l2_port_counters[l2_idx] += 1

        done = i + 1
        if done % 200 == 0 or done == total_clients:
            print(
                f"    Client L2 connections: {done}/{total_clients} devices "
                f"(created={created - pa_l2_created})"
            )

    # Edge-to-hub: each L2 edge connects to both hubs via eth47, eth48
    n_edges = len(l2_switch_ids)
    n_hubs = len(l2_hub_switch_ids)
    print(f"  Creating L2 edge-to-hub connections ({n_edges} x {n_hubs})...")
    l2_hub_port_counters = [0] * len(l2_hub_switch_ids)
    for edge_id in l2_switch_ids:
        for hub_idx, hub_id in enumerate(l2_hub_switch_ids):
            hub_port = l2_hub_port_counters[hub_idx] + 1
            resp = client.post(
                f"{BASE}/cabling/connections",
                json={
                    "device_a_id": edge_id,
                    "port_a": f"eth{47 + hub_idx}",
                    "device_b_id": hub_id,
                    "port_b": f"eth{hub_port}",
                    "connection_type": "ethernet",
                },
            )
            if resp.status_code == 201:
                created += 1
            l2_hub_port_counters[hub_idx] += 1

    # Hub-to-hub: single connection between the two L2 hubs
    if len(l2_hub_switch_ids) == 2:
        print("  Creating L2 hub-to-hub connection...")
        resp = client.post(
            f"{BASE}/cabling/connections",
            json={
                "device_a_id": l2_hub_switch_ids[0],
                "port_a": "eth47",
                "device_b_id": l2_hub_switch_ids[1],
                "port_b": "eth47",
                "connection_type": "ethernet",
            },
        )
        if resp.status_code == 201:
            created += 1

    return created
