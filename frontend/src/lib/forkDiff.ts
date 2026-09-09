import type { Edge, Node } from "@xyflow/react";
import type {
  CanvasData,
  CanvasNodeData,
  DeviceNodeData,
  L3RouteIntent,
  LayerEdgeData,
} from "@/types/topology.types";
import { isDeviceNode, l3RoutesOf } from "@/lib/canvasNodes";

// Pure client-side diff between two canvas_data payloads (issue #622). Mirrors
// the arithmetic cabling's fork save reconcile does server-side for wires: an
// edge's identity is its (source, target, source_port_name, target_port_name)
// tuple, never its own `id` field. A canvas edge's `id` is a client-generated
// genId() re-minted every time a line is redrawn (see TopologyEditorPage's
// stripTransientEdgeFields / WiringDialog), so two fork versions that both
// carry "the same wire" almost never share an edge id; keying on id would
// misreport every such save as churn. Nodes ARE keyed by their own id: unlike
// edges, a node's id is stable across saves for as long as the device stays on
// the canvas (it changes only on a genuine remove-then-re-add, which IS a real
// add/remove).
// One device node whose L3 routing intent (issue #34, ADR 0014) changed
// between the two canvases: present in both `before` and `after` (a node
// that only appears on one side is already reported via addedNodes/
// removedNodes, not here), with a different route SET (order-insensitive;
// see routeSetDiffKey below).
export interface RoutingChangedNode {
  node: Node<CanvasNodeData>;
  added: number;
  removed: number;
}

export interface ForkCanvasDiff {
  addedNodes: Node<CanvasNodeData>[];
  removedNodes: Node<CanvasNodeData>[];
  addedEdges: Edge<LayerEdgeData>[];
  removedEdges: Edge<LayerEdgeData>[];
  routingChangedNodes: RoutingChangedNode[];
}

export function edgeIdentityKey(edge: Edge<LayerEdgeData>): string {
  const data = edge.data as LayerEdgeData | undefined;
  return [edge.source, edge.target, data?.source_port_name ?? "", data?.target_port_name ?? ""].join(
    "::",
  );
}

const EMPTY_DIFF: ForkCanvasDiff = {
  addedNodes: [],
  removedNodes: [],
  addedEdges: [],
  removedEdges: [],
  routingChangedNodes: [],
};

// The route-set identity key the fork diff compares on (E6): order-
// insensitive across (destination, interface, next_hop), plus
// virtual_router, matching the brief's stated identity fields. Diffs the RAW
// text the way `edgeIdentityKey` diffs raw port names (review fix F7,
// issue #34): a client-side destination canonicalizer briefly lived here to
// mirror cabling's `str(ipaddress.ip_network(value, strict=False))`, on the
// premise that the server canonicalizes into `canvas_data` and two fork
// versions could therefore hold differently-formatted "same" routes. That
// premise was wrong: the server canonicalizes only into `fork_l3_routes`
// (the resolved-intent table), never back into `canvas_data`, so both
// canvases always hold the user's own raw text and there is no
// canonicalization-only difference to hide. The mirror was also a real
// re-implementation of `ipaddress.ip_network` that had already drifted from
// it in named cases (a `/255.255.255.0`-style dotted-decimal mask, a
// zero-padded prefix, an IPv6 zone id, `::` compression edge cases), each a
// possible false diff of its own. Removed entirely rather than fixed.
function routeSetDiffKey(route: L3RouteIntent): string {
  return [route.destination, route.interface, route.next_hop ?? "", route.virtual_router ?? ""].join(
    "|",
  );
}

function l3Routes(node: Node<CanvasNodeData> | undefined): L3RouteIntent[] {
  if (!node || !isDeviceNode(node)) return [];
  return l3RoutesOf(node.data as DeviceNodeData);
}

// Computes the routing-change summary for every node present in BOTH
// canvases (a node on only one side is already fully covered by
// addedNodes/removedNodes). Counts routes by SET membership (a route
// appearing twice with the same identity counts once), matching how the
// backend's own reconcile treats route identity.
function diffRoutingChangedNodes(
  beforeNodes: Node<CanvasNodeData>[],
  afterNodes: Node<CanvasNodeData>[],
): RoutingChangedNode[] {
  const beforeById = new Map(beforeNodes.map((n) => [n.id, n]));
  const result: RoutingChangedNode[] = [];
  for (const afterNode of afterNodes) {
    const beforeNode = beforeById.get(afterNode.id);
    if (!beforeNode) continue;
    const beforeKeys = new Set(l3Routes(beforeNode).map(routeSetDiffKey));
    const afterKeys = new Set(l3Routes(afterNode).map(routeSetDiffKey));
    if (beforeKeys.size === 0 && afterKeys.size === 0) continue;
    let added = 0;
    for (const key of afterKeys) if (!beforeKeys.has(key)) added++;
    let removed = 0;
    for (const key of beforeKeys) if (!afterKeys.has(key)) removed++;
    if (added > 0 || removed > 0) {
      result.push({ node: afterNode, added, removed });
    }
  }
  return result;
}

/**
 * Diffs `before` to `after`: "added" means present in `after` but not
 * `before`, "removed" means present in `before` but not `after`. A null/
 * undefined canvas is treated as empty (an unloaded or never-saved fork), so
 * diffing against nothing reports everything on the other side as added (or
 * removed), never throwing.
 */
export function diffForkCanvases(
  before: CanvasData | null | undefined,
  after: CanvasData | null | undefined,
): ForkCanvasDiff {
  if (!before && !after) return EMPTY_DIFF;

  const beforeNodes = before?.nodes ?? [];
  const afterNodes = after?.nodes ?? [];
  const beforeEdges = before?.edges ?? [];
  const afterEdges = after?.edges ?? [];

  const beforeNodeIds = new Set(beforeNodes.map((n) => n.id));
  const afterNodeIds = new Set(afterNodes.map((n) => n.id));
  const addedNodes = afterNodes.filter((n) => !beforeNodeIds.has(n.id));
  const removedNodes = beforeNodes.filter((n) => !afterNodeIds.has(n.id));

  const { addedEdges, removedEdges } = diffEdgesByKeyCount(beforeEdges, afterEdges);
  const routingChangedNodes = diffRoutingChangedNodes(beforeNodes, afterNodes);

  return { addedNodes, removedNodes, addedEdges, removedEdges, routingChangedNodes };
}

// Groups edges by identity key, preserving each key's edges in their
// original array order (so a "take the extra N" slice below picks a stable,
// arbitrary-but-deterministic representative when several edges share a key).
function groupEdgesByKey(edges: Edge<LayerEdgeData>[]): Map<string, Edge<LayerEdgeData>[]> {
  const groups = new Map<string, Edge<LayerEdgeData>[]>();
  for (const edge of edges) {
    const key = edgeIdentityKey(edge);
    const group = groups.get(key);
    if (group) {
      group.push(edge);
    } else {
      groups.set(key, [edge]);
    }
  }
  return groups;
}

/**
 * Multiset (count-based) edge diff, keyed by edgeIdentityKey. A plain Set of
 * keys collapses same-key duplicates into one membership test, so two edges
 * sharing a key (e.g. two lines with no recorded port names, common on a
 * canvas saved before issue #531 gave each line its own source_port_name/
 * target_port_name) would report zero change if only one of them were
 * removed. Comparing per-key COUNTS instead catches that: N before and M
 * after nets max(0, M - N) added and max(0, N - M) removed, each reported as
 * that many representative edges from the respective side (the edges are
 * identical by identity, so which specific instances stand in does not
 * change what the diff communicates).
 */
function diffEdgesByKeyCount(
  before: Edge<LayerEdgeData>[],
  after: Edge<LayerEdgeData>[],
): { addedEdges: Edge<LayerEdgeData>[]; removedEdges: Edge<LayerEdgeData>[] } {
  const beforeGroups = groupEdgesByKey(before);
  const afterGroups = groupEdgesByKey(after);
  const allKeys = new Set([...beforeGroups.keys(), ...afterGroups.keys()]);

  const addedEdges: Edge<LayerEdgeData>[] = [];
  const removedEdges: Edge<LayerEdgeData>[] = [];
  for (const key of allKeys) {
    const beforeGroup = beforeGroups.get(key) ?? [];
    const afterGroup = afterGroups.get(key) ?? [];
    const delta = afterGroup.length - beforeGroup.length;
    if (delta > 0) {
      addedEdges.push(...afterGroup.slice(afterGroup.length - delta));
    } else if (delta < 0) {
      const removedCount = -delta;
      removedEdges.push(...beforeGroup.slice(beforeGroup.length - removedCount));
    }
  }
  return { addedEdges, removedEdges };
}

// Builds a read-only render of `after` annotated with the diff so the canvas
// can highlight what changed (issue #622: "highlight added/removed edges on
// the canvas"). Nodes render as `after` unchanged (added/removed nodes are
// surfaced only in the panel's lists, not as canvas ghosts, matching the
// issue's scope). Edges in `after` that are new get `diffStatus: "added"`; a
// removed edge is synthesized back onto the canvas (`diffStatus: "removed"`)
// ONLY when both its endpoint nodes still exist in `after` (its own two nodes
// may have been removed too, in which case React Flow has nothing to anchor
// it to and it stays panel-only).
export function buildForkDiffOverlayCanvas(
  after: CanvasData | null | undefined,
  diff: ForkCanvasDiff,
): CanvasData {
  const nodes = after?.nodes ?? [];
  const nodeIds = new Set(nodes.map((n) => n.id));
  const addedEdgeIds = new Set(diff.addedEdges.map((e) => e.id));

  const edges: Edge<LayerEdgeData>[] = (after?.edges ?? []).map((e) =>
    addedEdgeIds.has(e.id)
      ? { ...e, data: { ...(e.data as LayerEdgeData), diffStatus: "added" } }
      : e,
  );

  for (const removed of diff.removedEdges) {
    if (!nodeIds.has(removed.source) || !nodeIds.has(removed.target)) continue;
    edges.push({
      ...removed,
      id: `diff-removed-${removed.id}`,
      selected: false,
      data: { ...(removed.data as LayerEdgeData), diffStatus: "removed" },
    });
  }

  return { nodes, edges, selectedEdgeLayer: after?.selectedEdgeLayer };
}
