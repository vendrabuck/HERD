import uuid
from collections import deque

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.connection import Connection
from app.schemas.pathfind import PathHop


async def build_adjacency_graph(
    db: AsyncSession,
) -> dict[uuid.UUID, list[tuple[uuid.UUID, str, str]]]:
    """Load all connections and build a bidirectional adjacency list.

    Each entry maps device_id to a list of (neighbor_id, local_port, remote_port).
    """
    result = await db.execute(
        select(
            Connection.device_a_id,
            Connection.port_a,
            Connection.device_b_id,
            Connection.port_b,
        )
    )
    rows = result.all()

    graph: dict[uuid.UUID, list[tuple[uuid.UUID, str, str]]] = {}
    for device_a_id, port_a, device_b_id, port_b in rows:
        graph.setdefault(device_a_id, []).append((device_b_id, port_a, port_b))
        graph.setdefault(device_b_id, []).append((device_a_id, port_b, port_a))
    return graph


def find_all_shortest_paths(
    graph: dict[uuid.UUID, list[tuple[uuid.UUID, str, str]]],
    source_id: uuid.UUID,
    target_id: uuid.UUID,
) -> list[list[PathHop]]:
    """Find every shortest route between two devices.

    BFS with multi-predecessor tracking enumerates all minimum-hop paths.
    Routes that traverse the same intermediate device sequence via different
    cables collapse to a single entry: parallel cables between the same pair
    of devices look like one route to the user, not N. Edge weights are
    uniform (1 hop). Returns an empty list when target is unreachable.
    """
    if source_id == target_id:
        return [[PathHop(device_id=source_id)]]

    if source_id not in graph:
        return []

    # distance: device -> shortest hop count from source
    # predecessors: device -> list of (prev_device, port_out_from_prev, port_in_to_current)
    distance: dict[uuid.UUID, int] = {source_id: 0}
    predecessors: dict[uuid.UUID, list[tuple[uuid.UUID, str, str]]] = {}
    queue: deque[uuid.UUID] = deque([source_id])

    while queue:
        current = queue.popleft()
        if current == target_id:
            # Continue draining queue at the target's level so we capture all
            # equal-cost predecessors, but no further BFS expansion is needed.
            continue
        for neighbor_id, local_port, remote_port in graph.get(current, []):
            new_cost = distance[current] + 1
            if neighbor_id not in distance:
                distance[neighbor_id] = new_cost
                predecessors[neighbor_id] = [(current, local_port, remote_port)]
                queue.append(neighbor_id)
            elif new_cost == distance[neighbor_id]:
                predecessors[neighbor_id].append((current, local_port, remote_port))

    if target_id not in distance:
        return []

    all_paths = _enumerate_paths(predecessors, source_id, target_id)
    # Dedupe by intermediate-device sequence: parallel cables through the
    # same hops are one route to the operator, even if the cabling DB has N.
    seen: set[tuple[uuid.UUID, ...]] = set()
    unique: list[list[PathHop]] = []
    for path in all_paths:
        key = tuple(hop.device_id for hop in path[1:-1])
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def _enumerate_paths(
    predecessors: dict[uuid.UUID, list[tuple[uuid.UUID, str, str]]],
    source_id: uuid.UUID,
    target_id: uuid.UUID,
) -> list[list[PathHop]]:
    """Walk every predecessor chain from target back to source."""
    # Each entry on the stack: (current_device, hops_so_far_from_target)
    # hops_so_far_from_target stores tuples (device, port_in, port_out_to_next)
    paths: list[list[PathHop]] = []
    stack: list[tuple[uuid.UUID, list[tuple[uuid.UUID, str | None, str | None]]]] = [
        (target_id, [(target_id, None, None)])
    ]

    while stack:
        current, trail = stack.pop()
        if current == source_id:
            ordered = list(reversed(trail))
            hops = [
                PathHop(device_id=did, port_in=port_in, port_out=port_out)
                for did, port_in, port_out in ordered
            ]
            paths.append(hops)
            continue
        for prev_id, port_out_from_prev, port_in_to_current in predecessors.get(current, []):
            # Update the existing tail's port_in (target side of this edge).
            new_trail = trail.copy()
            tail_device, _, tail_port_out = new_trail[-1]
            new_trail[-1] = (tail_device, port_in_to_current, tail_port_out)
            new_trail.append((prev_id, None, port_out_from_prev))
            stack.append((prev_id, new_trail))

    return paths
