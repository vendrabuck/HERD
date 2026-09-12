"""Topology canvases: the React Flow canvas builder, the named shape catalog,
and the 50-valid / 10-invalid demo topology seed.
"""

import httpx

from .catalog import INVALID_TOPOLOGY_TARGET, VALID_TOPOLOGY_TARGET
from .client import BASE, fetch_all_items


def list_existing_topology_names(client: httpx.Client) -> set[str]:
    """Return the set of all existing topology names (for idempotency)."""
    items = fetch_all_items(client, f"{BASE}/cabling/topologies")
    return {t["name"] for t in items}


def get_or_create_topology(
    client: httpx.Client,
    name: str,
    canvas_data: dict,
    existing_names: set[str],
    description: str | None = None,
) -> str | None:
    """Create a topology by name if absent, then set its canvas_data.

    Returns the new topology id, or None if it already existed (skipped) or
    creation failed. Mutates existing_names to include freshly created names.
    """
    if name in existing_names:
        print(f"  Exists topology: {name}")
        return None

    resp = client.post(f"{BASE}/cabling/topologies", json={"name": name})
    if resp.status_code != 201:
        print(f"  Failed to create topology {name} ({resp.status_code}): {resp.text}")
        return None
    tid = resp.json()["id"]

    put_body: dict = {"canvas_data": canvas_data}
    if description is not None:
        put_body["description"] = description
    put_resp = client.put(f"{BASE}/cabling/topologies/{tid}", json=put_body)
    if put_resp.status_code != 200:
        print(f"  Created topology {name} but canvas PUT failed ({put_resp.status_code})")
    else:
        print(f"  Created topology: {name}")
    existing_names.add(name)
    return tid


# Device-record fields the frontend DeviceNode reads. Mirrors the DeviceNodeData
# shape so seeded nodes are indistinguishable from hand-built (live-dropped) ones.
CANVAS_DEVICE_FIELDS = (
    "id",
    "name",
    "topology_type",
    "template_name",
    "template_icon",
    "status",
)


def _node_device_payload(device: dict) -> dict:
    """Project a full inventory device record onto the canvas device shape.

    Keeps only the fields DeviceNode renders, falling back to safe defaults for
    any the API omits so the node never carries a null where the component
    expects a value (name span, topology color, status badge).
    """
    payload = {field: device.get(field) for field in CANVAS_DEVICE_FIELDS}
    payload["id"] = device.get("id")
    payload["name"] = device.get("name") or ""
    payload["topology_type"] = device.get("topology_type") or "PHYSICAL"
    payload["status"] = device.get("status") or "AVAILABLE"
    return payload


def build_canvas(
    device_ids: list[str | None],
    edges: list[tuple[int, int, str]],
    device_lookup: dict[str, dict] | None = None,
) -> dict:
    """Build a React Flow canvas_data dict mirroring the editor output.

    device_ids[i] is the device UUID for node i, or None to emit a node with
    empty data (which forces missing_device on any edge touching it). edges is a
    list of (source_index, target_index, layer) tuples. Node ids are React Flow
    ids ("n0", "n1", ...), distinct from device UUIDs; edges reference node ids.
    Positions are laid out on a deterministic grid so the canvas renders sanely.

    device_lookup maps device UUID to the full inventory record. When present,
    each non-None node gets the full device payload (name, topology_type,
    template_name, status, ...) plus the same label/topologyType the live drop
    path sets, and "type": "deviceNode" so React Flow renders the custom
    DeviceNode instead of its blank default node. A node whose device id is None
    (or missing from the lookup) emits an empty- or thin-data node, still typed
    "deviceNode"; a None slot intentionally forces missing_device on any edge
    touching it.
    """
    device_lookup = device_lookup or {}
    nodes: list[dict] = []
    for i, dev_id in enumerate(device_ids):
        node: dict = {
            "id": f"n{i}",
            "type": "deviceNode",
            "position": {"x": 100 + (i % 4) * 200, "y": 100 + (i // 4) * 150},
        }
        if dev_id is None:
            node["data"] = {}
        else:
            device = device_lookup.get(dev_id)
            if device is None:
                # No record available: keep a thin reference so the load-time
                # hydration in the editor can still fill it in by id.
                node["data"] = {"device": {"id": dev_id}}
            else:
                payload = _node_device_payload(device)
                node["data"] = {
                    "device": payload,
                    "label": payload["name"],
                    "topologyType": payload["topology_type"],
                }
        nodes.append(node)

    edge_list: list[dict] = []
    for j, (source, target, layer) in enumerate(edges):
        edge_list.append(
            {
                "id": f"e{j}",
                "source": f"n{source}",
                "target": f"n{target}",
                "data": {"layer": layer, "isProposal": False},
            }
        )
    return {"nodes": nodes, "edges": edge_list}


def _chain_edges(n: int, layer: str) -> list[tuple[int, int, str]]:
    """Linear chain: 0-1-2-...-(n-1)."""
    return [(i, i + 1, layer) for i in range(n - 1)]


def _star_edges(n: int, layer: str) -> list[tuple[int, int, str]]:
    """Star: node 0 is the hub, all others connect to it."""
    return [(0, i, layer) for i in range(1, n)]


def _ring_edges(n: int, layer: str) -> list[tuple[int, int, str]]:
    """Ring: chain with a wrap-around edge back to node 0."""
    return [(i, (i + 1) % n, layer) for i in range(n)]


def _full_mesh_edges(n: int, layer: str) -> list[tuple[int, int, str]]:
    """Full mesh: every pair of nodes connected once."""
    return [(a, b, layer) for a in range(n) for b in range(a + 1, n)]


def _dual_homed_edges(n: int, layer: str) -> list[tuple[int, int, str]]:
    """Dual-homed: nodes 0,1 are cores; 2,3 are leaves, each to both cores."""
    return [(2, 0, layer), (2, 1, layer), (3, 0, layer), (3, 1, layer)]


# Curated named shapes: (name, node_count, edge_generator, layer). Expanded into
# exactly VALID_TOPOLOGY_TARGET topologies by cycling sets and slicing the device
# pool by a rolling offset, so each variant uses different devices.
SHAPE_CATALOG = [
    ("Dual-Homed Pair", 4, _dual_homed_edges, "L1"),
    ("Linear Chain 3-Node", 3, _chain_edges, "L2"),
    ("Linear Chain 5-Node", 5, _chain_edges, "L2"),
    ("Star - Core Switch", 5, _star_edges, "L1"),
    ("Star - Access Layer", 6, _star_edges, "L2"),
    ("L2 Access Ring", 5, _ring_edges, "L2"),
    ("Spine-Leaf 4-Node", 4, _dual_homed_edges, "L3"),
    ("Three-Tier Web/App/DB", 3, _chain_edges, "L3"),
    ("Firewall Sandwich", 3, _chain_edges, "L3"),
    ("Full Mesh Quad", 4, _full_mesh_edges, "L2"),
    ("Point-to-Point Link", 2, _chain_edges, "L1"),
    ("Hub-and-Spoke 6", 6, _star_edges, "L1"),
    ("Backbone Ring 6", 6, _ring_edges, "L2"),
    ("Collapsed Core Pair", 2, _chain_edges, "L3"),
    ("Edge-to-Hub Uplink", 2, _chain_edges, "L1"),
    ("Leaf Triangle", 3, _ring_edges, "L2"),
    ("Quad Mesh Core", 4, _full_mesh_edges, "L3"),
    ("Two-Tier Distribution", 5, _star_edges, "L2"),
]


def seed_topologies(
    client: httpx.Client,
    edge_switch_ids: list[str],
    hub_switch_ids: list[str],
    l2_switch_ids: list[str],
    l2_hub_switch_ids: list[str],
    isolated_device_ids: list[str],
) -> tuple[int, int]:
    """Seed exactly 50 valid and 10 invalid demo lab topologies.

    Returns (valid_count, invalid_count), counting intended topologies whether
    freshly created or already present, so the summary is stable across re-runs.
    """
    existing_names = list_existing_topology_names(client)

    # Build an id to full-record lookup once, so every canvas node carries the
    # device fields the frontend DeviceNode renders (name, topology_type,
    # template_name, status). The inventory API is the source of truth here, the
    # same data the editor's load-time hydration fetches. Devices missing from
    # the map degrade to a thin {"device": {"id": ...}} node (still hydratable).
    device_lookup: dict[str, dict] = {
        d["id"]: d for d in fetch_all_items(client, f"{BASE}/inventory/devices")
    }

    # Pool of guaranteed-cabled, mutually reachable devices: switch infrastructure
    # ONLY. Every L1/L2 edge and hub switch is deterministically cabled and the
    # whole fabric is one connected component (verified live), so any two switches
    # are mutually reachable. DUTs are deliberately NOT included: the L1 cabling
    # pass exhausts edge-switch ports, so many DUTs (including front-of-list
    # clients) end up uncabled and would make a "valid" topology validate as
    # no_path. 61 switches is ample for 50 topologies of at most 6 nodes.
    cabled_pool = edge_switch_ids + hub_switch_ids + l2_switch_ids + l2_hub_switch_ids
    if len(cabled_pool) < 2:
        print("  Skipping topologies: fewer than 2 cabled devices available")
        return (0, 0)

    pool_len = len(cabled_pool)
    valid_count = 0
    offset = 0
    first_valid_id: str | None = None
    for set_num in range(1, 99):
        if valid_count >= VALID_TOPOLOGY_TARGET:
            break
        for base_name, n, gen, layer in SHAPE_CATALOG:
            if valid_count >= VALID_TOPOLOGY_TARGET:
                break
            name = base_name if set_num == 1 else f"{base_name} (Set {set_num})"
            node_devices: list[str | None] = [
                cabled_pool[(offset + i) % pool_len] for i in range(n)
            ]
            canvas = build_canvas(node_devices, gen(n, layer), device_lookup)
            tid = get_or_create_topology(
                client, name, canvas, existing_names, description="Demo lab topology"
            )
            if first_valid_id is None and tid is not None:
                first_valid_id = tid
            valid_count += 1
            offset = (offset + n) % pool_len

    # Invalid topologies. no_path uses an isolated (uncabled) endpoint;
    # missing_device uses a node with empty data (None slot).
    invalid_specs: list[tuple[str, list[str | None], list[tuple[int, int, str]]]] = []

    def _iso(i: int) -> str:
        return isolated_device_ids[i % len(isolated_device_ids)]

    if isolated_device_ids:
        invalid_specs.extend(
            [
                (
                    "BROKEN - Unreachable Cross-Fabric",
                    [cabled_pool[0], _iso(0)],
                    [(0, 1, "L2")],
                ),
                ("BROKEN - Isolated Leaf", [cabled_pool[1 % pool_len], _iso(1)], [(0, 1, "L1")]),
                ("BROKEN - Orphan Node Link", [_iso(2), _iso(3)], [(0, 1, "L3")]),
                ("BROKEN - Dangling Uplink", [cabled_pool[2 % pool_len], _iso(4)], [(0, 1, "L1")]),
                (
                    "BROKEN - Partial Mesh Gap",
                    [cabled_pool[3 % pool_len], cabled_pool[4 % pool_len], _iso(5)],
                    [(0, 1, "L2"), (1, 2, "L2")],
                ),
                ("BROKEN - Stranded Pair", [_iso(0), _iso(1)], [(0, 1, "L2")]),
            ]
        )
    invalid_specs.extend(
        [
            ("BROKEN - Missing Device Ref", [cabled_pool[0], None], [(0, 1, "L2")]),
            ("BROKEN - Empty Node", [None, cabled_pool[1 % pool_len]], [(0, 1, "L1")]),
            ("BROKEN - Two Empty Nodes", [None, None], [(0, 1, "L3")]),
            (
                "BROKEN - Half-Wired Chain",
                [cabled_pool[2 % pool_len], None, cabled_pool[3 % pool_len]],
                [(0, 1, "L2"), (1, 2, "L2")],
            ),
        ]
    )

    invalid_count = 0
    first_invalid_id: str | None = None
    for name, node_devices, edges in invalid_specs[:INVALID_TOPOLOGY_TARGET]:
        canvas = build_canvas(node_devices, edges, device_lookup)
        tid = get_or_create_topology(
            client, name, canvas, existing_names, description="Deliberately invalid demo topology"
        )
        if first_invalid_id is None and tid is not None:
            first_invalid_id = tid
        invalid_count += 1

    # Best-effort self-check: validate one fresh sample of each kind.
    if first_valid_id and first_invalid_id:
        v = client.post(f"{BASE}/cabling/topologies/{first_valid_id}/validate")
        iv = client.post(f"{BASE}/cabling/topologies/{first_invalid_id}/validate")
        if v.status_code == 200 and iv.status_code == 200:
            print(
                f"  Self-check: sample valid -> valid={v.json()['valid']}, "
                f"sample invalid -> valid={iv.json()['valid']}"
            )

    print(f"  {valid_count} valid, {invalid_count} invalid topologies")
    return (valid_count, invalid_count)
