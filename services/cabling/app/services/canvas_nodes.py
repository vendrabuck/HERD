"""Canvas node/element resolution: the leaf module behind every canvas reader.

Moved out of ``fork_save_service.py`` (R1 review fix on 2ade362c, ADR 0014 phase 1,
issue #34) so it has zero dependency on anything else in ``app.services`` or
``app.routes``. Every other canvas-reading module (``l3_intent.py``,
``topology_validation.py``, ``fork_save_service.py``, ``fork_service.py``) imports
from here instead of from each other, which is what let the fork write paths drop
their function-local "avoid a cycle" imports: there is no cycle left to avoid.

``fork_save_service`` re-exports ``node_to_device_map``, ``node_to_element_map``, and
``classify_element_edge`` for existing importers of that module.

``strip_device_nodes`` (with its ``DEVICE_NODE_ALLOWED_KEYS`` allowlist) is the one
place every cabling write boundary reduces a device node's ``data.device`` to what the
codebase actually reads off it, so a device's ``field_data`` (which can carry
clear-text credentials) never enters a stored canvas.
"""

import uuid

# The only keys of a device node's `data.device` that cabling may persist and
# return, derived from every read site across the frontend and backend (a
# hardening pass, not a new feature): an inventory device record is fetched
# fresh by id wherever real detail is needed (there are no cross-schema
# FK/JOINs in HERD), so a canvas node only ever needs enough of the device to
# render itself and to let the few callers below key off it.
#
# - "id": the one key the backend itself reads off a device node
#   (node_to_device_map above, fork_save_service's removed-id check, and
#   routes/templates.py's instantiate flow, which writes it onto a role
#   placeholder).
# - "name": frontend/src/lib/canvasNodes.ts canvasNodeLabel,
#   components/topology-editor/RoutingPanel.tsx, nodes/DeviceNode.tsx.
# - "topology_type": nodes/DeviceNode.tsx's color class, and
#   pages/TopologyEditorPage.tsx's cross-type (PHYSICAL/CLOUD) edge guard.
# - "connection_type": lib/l3.ts's isLayer3Switch, which gates whether the
#   Routing panel and the route-count badge apply to this node at all.
# - "status": nodes/DeviceNode.tsx's non-AVAILABLE badge.
# - "template_name": nodes/DeviceNode.tsx's caption, and
#   routes/templates.py's _extract_role_template, which reads it to name a
#   template role.
# - "template_icon": nodes/DeviceNode.tsx's icon image.
# - "role": never a real inventory device field; the placeholder key
#   routes/templates.py writes in place of a device (_extract_role_template)
#   and reads back at instantiation (_instantiate_canvas) for a
#   topology-template's canvas, which reuses this same device-node shape.
#
# Deliberately excluded for lack of any read site: template_id,
# template_vendor, template_model, template_part_number, driver_id,
# driver_name, driver_sha256, driver_filename, exclusive, created_at,
# created_by, created_by_name, modified_by, modified_by_name,
# poll_interval_seconds, resolved_poll_interval_seconds, and, above all,
# field_data (which can carry clear-text device credentials) and anything
# else credential-shaped. A device node never carries a "ports" key either
# (Device has none; ports are fetched separately by device id), so there is
# nothing to allow there.
DEVICE_NODE_ALLOWED_KEYS = frozenset(
    {
        "id",
        "name",
        "topology_type",
        "connection_type",
        "status",
        "template_name",
        "template_icon",
        "role",
    }
)


def strip_device_nodes(canvas: dict) -> dict:
    """Return a copy of ``canvas`` with every device node's ``data.device``
    reduced to ``DEVICE_NODE_ALLOWED_KEYS``.

    Applied at every cabling write boundary (topology create/update/clone,
    bulk import, template create/update/instantiate, and every fork write) so
    a device node's ``field_data`` and any other non-allowlisted key never
    reaches a stored canvas, regardless of what a caller (editor, import
    file, or a raw API request) sent.

    Never mutates ``canvas``. A node is treated as a device node purely by
    the shape of its own data (an object under ``data.device``), the same
    test the rest of this module uses, not by its ``type`` tag: this also
    catches a legacy thin node saved before the seed fix that set the
    ``deviceNode`` type discriminator. Every other node (network elements,
    dynamic placeholders), every edge, and every other top-level canvas key
    (e.g. ``viewport``) pass through untouched, by reference. A node that is
    not a dict, or whose ``data``/``data.device`` is not a dict, is returned
    unchanged: narrowing a device dict is this function's only job, not
    validating canvas shape. Returns ``canvas`` itself, unchanged, when
    nothing needed stripping (idempotent: a second pass over already-stripped
    data is always a no-op).
    """
    if not isinstance(canvas, dict):
        return canvas
    nodes = canvas.get("nodes")
    if not isinstance(nodes, list):
        return canvas

    new_nodes = []
    changed = False
    for node in nodes:
        if not isinstance(node, dict):
            new_nodes.append(node)
            continue
        data = node.get("data")
        device = data.get("device") if isinstance(data, dict) else None
        if not isinstance(device, dict):
            new_nodes.append(node)
            continue
        stripped_device = {k: v for k, v in device.items() if k in DEVICE_NODE_ALLOWED_KEYS}
        if stripped_device.keys() == device.keys():
            new_nodes.append(node)
            continue
        changed = True
        new_nodes.append({**node, "data": {**data, "device": stripped_device}})

    if not changed:
        return canvas
    return {**canvas, "nodes": new_nodes}


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
    "DEVICE_NODE_ALLOWED_KEYS",
    "node_to_device_map",
    "node_to_element_map",
    "redact_invisible_device_nodes",
    "strip_device_nodes",
]
