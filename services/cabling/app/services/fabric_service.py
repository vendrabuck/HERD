"""Fabric detection: identify connected components in the cabling graph.

Two devices in the same connected component are in the same physical fabric,
meaning a VLAN on one can reach the other. Devices in separate components
are isolated and can safely reuse the same VLAN ID.
"""

import uuid
from collections import deque


def find_connected_component(
    graph: dict[uuid.UUID, list[tuple[uuid.UUID, str, str]]],
    device_id: uuid.UUID,
) -> set[uuid.UUID]:
    """BFS from device_id to find all devices in its connected component."""
    if device_id not in graph:
        return {device_id}

    visited: set[uuid.UUID] = set()
    queue: deque[uuid.UUID] = deque([device_id])

    while queue:
        current = queue.popleft()
        if current in visited:
            continue
        visited.add(current)
        for neighbor_id, _, _ in graph.get(current, []):
            if neighbor_id not in visited:
                queue.append(neighbor_id)

    return visited


def compute_fabric_id(component: set[uuid.UUID]) -> uuid.UUID:
    """Derive a deterministic fabric UUID from the sorted member device UUIDs."""
    sorted_ids = sorted(str(uid) for uid in component)
    return uuid.uuid5(uuid.NAMESPACE_DNS, "|".join(sorted_ids))
