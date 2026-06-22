import asyncio
import uuid
from collections import deque

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.connection import Connection
from app.schemas.pathfind import PathHop

# Hard ceiling on enumerated shortest paths returned for a single query. Dense
# fabrics (many parallel equal-cost routes through distinct intermediates) can
# blow up _enumerate_paths combinatorially; we truncate to keep latency and
# memory bounded. The post-enumeration dedupe collapses parallel cables through
# the same intermediates, so this cap bites only on genuinely distinct routes.
MAX_ENUMERATED_PATHS = 256


async def build_adjacency_graph(
    db: AsyncSession,
    device_ids: set[uuid.UUID] | None = None,
) -> dict[uuid.UUID, list[tuple[uuid.UUID, str, str]]]:
    """Load connections and build a bidirectional adjacency list.

    Each entry maps device_id to a list of (neighbor_id, local_port, remote_port).

    When ``device_ids`` is None the whole connections table is loaded (the
    original, instance-wide behavior). When a set is given, only the connected
    components that contain those devices are loaded: we expand the device
    frontier iteratively (each round pulls every connection touching a known
    device, then adds the newly discovered neighbors) until it stops growing.
    That keeps the load scoped to the relevant fabric(s) without ever dropping a
    legitimate path, since every edge reachable from a seed device is loaded,
    including intermediates that are not themselves in ``device_ids`` (for
    example a patch panel that physically realizes a topology edge).
    """
    graph: dict[uuid.UUID, list[tuple[uuid.UUID, str, str]]] = {}

    def _add(device_a_id, port_a, device_b_id, port_b):
        graph.setdefault(device_a_id, []).append((device_b_id, port_a, port_b))
        graph.setdefault(device_b_id, []).append((device_a_id, port_b, port_a))

    if device_ids is None:
        rows = (
            await db.execute(
                select(
                    Connection.device_a_id,
                    Connection.port_a,
                    Connection.device_b_id,
                    Connection.port_b,
                )
            )
        ).all()
        for device_a_id, port_a, device_b_id, port_b in rows:
            _add(device_a_id, port_a, device_b_id, port_b)
        return graph

    if not device_ids:
        return graph

    # Iterative connected-component expansion. ``known`` is every device whose
    # incident edges are already loaded; ``frontier`` is the set queried this
    # round. Both device_a_id and device_b_id are indexed, so each round is an
    # indexed lookup, and the round count is bounded by the component diameter.
    known: set[uuid.UUID] = set()
    seen_edges: set[uuid.UUID] = set()
    frontier: set[uuid.UUID] = set(device_ids)

    while frontier:
        rows = (
            await db.execute(
                select(
                    Connection.id,
                    Connection.device_a_id,
                    Connection.port_a,
                    Connection.device_b_id,
                    Connection.port_b,
                ).where(
                    or_(
                        Connection.device_a_id.in_(frontier),
                        Connection.device_b_id.in_(frontier),
                    )
                )
            )
        ).all()

        known |= frontier
        discovered: set[uuid.UUID] = set()
        for conn_id, device_a_id, port_a, device_b_id, port_b in rows:
            if conn_id not in seen_edges:
                seen_edges.add(conn_id)
                _add(device_a_id, port_a, device_b_id, port_b)
            for endpoint in (device_a_id, device_b_id):
                if endpoint not in known:
                    discovered.add(endpoint)

        frontier = discovered - known

    return graph


async def find_all_shortest_paths_async(
    graph: dict[uuid.UUID, list[tuple[uuid.UUID, str, str]]],
    source_id: uuid.UUID,
    target_id: uuid.UUID,
) -> list[list[PathHop]]:
    """Offload the CPU-bound BFS + path enumeration to a worker thread.

    ``find_all_shortest_paths`` and ``_enumerate_paths`` are pure-Python and can
    be expensive on large or dense graphs; running them on the event loop would
    stall every other cabling request. This wrapper keeps the sync functions
    sync and pushes the work to ``asyncio.to_thread`` so the loop stays free.
    """
    return await asyncio.to_thread(find_all_shortest_paths, graph, source_id, target_id)


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

    The number of distinct returned routes is capped at MAX_ENUMERATED_PATHS;
    on a denser fabric the result is truncated rather than enumerated without
    bound.
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
        if len(unique) >= MAX_ENUMERATED_PATHS:
            break
    return unique


def _enumerate_paths(
    predecessors: dict[uuid.UUID, list[tuple[uuid.UUID, str, str]]],
    source_id: uuid.UUID,
    target_id: uuid.UUID,
) -> list[list[PathHop]]:
    """Walk predecessor chains from target back to source.

    Enumeration is deduped by intermediate-device sequence as it runs and stops
    once MAX_ENUMERATED_PATHS distinct sequences have been recorded, so a dense
    predecessor DAG (many parallel equal-cost routes) cannot blow up
    exponentially in either time or memory. Parallel cables through the same
    intermediates share a key and collapse to the first trail seen, which still
    carries valid ports for that route; the caller dedupes again on the same key
    (idempotent). The bound therefore counts genuinely distinct routes.
    """
    # Each entry on the stack: (current_device, hops_so_far_from_target)
    # hops_so_far_from_target stores tuples (device, port_in, port_out_to_next)
    paths: list[list[PathHop]] = []
    seen_keys: set[tuple[uuid.UUID, ...]] = set()
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
            key = tuple(hop.device_id for hop in hops[1:-1])
            if key in seen_keys:
                continue
            seen_keys.add(key)
            paths.append(hops)
            if len(seen_keys) >= MAX_ENUMERATED_PATHS:
                break
            continue
        for prev_id, port_out_from_prev, port_in_to_current in predecessors.get(current, []):
            # Update the existing tail's port_in (target side of this edge).
            new_trail = trail.copy()
            tail_device, _, tail_port_out = new_trail[-1]
            new_trail[-1] = (tail_device, port_in_to_current, tail_port_out)
            new_trail.append((prev_id, None, port_out_from_prev))
            stack.append((prev_id, new_trail))

    return paths
