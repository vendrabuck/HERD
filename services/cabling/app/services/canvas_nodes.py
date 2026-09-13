"""Canvas node/element resolution: the leaf module behind every canvas reader.

Moved out of ``fork_save_service.py`` (R1 review fix on 2ade362c, ADR 0014 phase 1,
issue #34) so it has zero dependency on anything else in ``app.services`` or
``app.routes``. Every other canvas-reading module (``l3_intent.py``,
``topology_validation.py``, ``fork_save_service.py``, ``fork_service.py``) imports
from here instead of from each other, which is what let the fork write paths drop
their function-local "avoid a cycle" imports: there is no cycle left to avoid.

``fork_save_service`` re-exports ``node_to_device_map``, ``node_to_element_map``, and
``classify_element_edge`` for existing importers of that module.
"""

import uuid


def node_to_device_map(canvas: dict) -> dict[str, uuid.UUID]:
    """Map React Flow node ids to device UUIDs, mirroring the topology validator."""
    nodes = canvas.get("nodes") or []
    mapping: dict[str, uuid.UUID] = {}
    for node in nodes:
        node_id = node.get("id")
        device_id_str = ((node.get("data") or {}).get("device") or {}).get("id")
        if not node_id or not device_id_str:
            continue
        try:
            mapping[node_id] = uuid.UUID(device_id_str)
        except (ValueError, TypeError):
            continue
    return mapping


def node_to_element_map(canvas: dict) -> dict[str, str]:
    """Map React Flow node ids to network element ids (ADR 0012 phase 1, issue #22).

    Populated from nodes whose ``type`` is ``"networkElementNode"``, keyed by node id,
    valued by ``data.element.id`` (the client-minted element UUID). A node of that type
    with no ``data.element.id`` falls back to the node id itself, so a malformed element
    node still classifies as an element rather than silently vanishing from the map.
    Shared by the topology validator and ``resolve_canvas_wiring`` so both classify an
    element edge identically.
    """
    nodes = canvas.get("nodes") or []
    mapping: dict[str, str] = {}
    for node in nodes:
        if node.get("type") != "networkElementNode":
            continue
        node_id = node.get("id")
        if not node_id:
            continue
        element_id = ((node.get("data") or {}).get("element") or {}).get("id")
        mapping[node_id] = element_id or node_id
    return mapping


def classify_element_edge(
    edge: dict,
    node_to_device: dict[str, uuid.UUID],
    node_to_element: dict[str, str],
) -> str | None:
    """Classify one canvas edge against the element/device maps (ADR 0012 phase 1).

    The single shared classifier behind both the topology validator and
    ``resolve_canvas_wiring``, so the validator and the fork-save resolver agree on
    which edges are element attachments and which of those are valid. Direction is
    accepted either way: the frontend normalizes device-as-source, but an older client
    or a hand-edited import may hand back the element first.

    Returns one of:

    - ``"attachment"``: exactly one endpoint is a network element, the other is a
      known device, and the device-side port name (``target_port_name`` when the
      device is the target, ``source_port_name`` when the device is the source) is
      non-empty. This is the only shape the topology validator accepts and the only
      shape ``resolve_canvas_wiring`` should count.
    - ``"element_to_element"``: both endpoints are network elements.
    - ``"element_edge_no_port"``: exactly one endpoint is a network element, the other
      is a known device, but the device-side port name is missing or empty.
    - ``None``: not an element edge (neither endpoint is in ``node_to_element``), OR
      exactly one endpoint is an element and the other resolves to no known device
      either. That second case is deliberately left for the caller's own
      missing-device/unresolvable-endpoint handling, since a dangling node reference
      is a dangling node reference regardless of what the other end is.
    """
    source_node = edge.get("source")
    target_node = edge.get("target")
    source_is_element = source_node in node_to_element
    target_is_element = target_node in node_to_element

    if not source_is_element and not target_is_element:
        return None

    if source_is_element and target_is_element:
        return "element_to_element"

    source_device = node_to_device.get(source_node) if source_node else None
    target_device = node_to_device.get(target_node) if target_node else None
    device_side_id = target_device if source_is_element else source_device
    if device_side_id is None:
        return None

    edge_data = edge.get("data") or {}
    device_side_port = (
        edge_data.get("target_port_name")
        if source_is_element
        else edge_data.get("source_port_name")
    )
    if not device_side_port:
        return "element_edge_no_port"

    return "attachment"


def redact_invisible_device_nodes(
    canvas: dict | None,
    visible_device_ids: set[uuid.UUID],
) -> dict | None:
    """Drop every device node whose device is outside ``visible_device_ids``.

    Issue #763: the user-facing topology validate route answers, per device id
    in the caller's own canvas, whether the device is reachable and (since ADR
    0014 phase 1) what its interfaces and subnets are. Non-admin device
    visibility is device-group gated everywhere else, so for a non-admin caller
    the canvas is redacted here BEFORE validation runs, rather than teaching
    each pass its own visibility rule.

    A redacted node is removed outright, so every downstream reader
    (``node_to_device_map``, ``walk_l3_nodes``, ``resolve_canvas_wiring``, the
    edge BFS, the response's ``device_ids``) treats it exactly as it treats a
    node that is not on the canvas at all: an edge touching it is reported with
    the existing ``missing_device`` reason, which is what an edge to a node with
    no resolvable device already produces, and its ``data.l3`` is never parsed,
    so no inventory config lookup is made for it.

    Returns the canvas unchanged (the same object) when nothing is hidden.
    Otherwise returns a shallow copy with a filtered ``nodes`` list; the caller
    hands us a persisted ORM value, so nothing here mutates the input.
    """
    if not canvas:
        return canvas
    nodes = canvas.get("nodes") or []
    device_map = node_to_device_map(canvas)
    kept = [
        node
        for node in nodes
        if device_map.get(node.get("id")) is None
        or device_map[node.get("id")] in visible_device_ids
    ]
    if len(kept) == len(nodes):
        return canvas
    return {**canvas, "nodes": kept}


__all__ = [
    "classify_element_edge",
    "node_to_device_map",
    "node_to_element_map",
    "redact_invisible_device_nodes",
]
