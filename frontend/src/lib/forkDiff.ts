import type { Edge, Node } from "@xyflow/react";
import type { CanvasData, CanvasNodeData, LayerEdgeData } from "@/types/topology.types";

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
export interface ForkCanvasDiff {
  addedNodes: Node<CanvasNodeData>[];
  removedNodes: Node<CanvasNodeData>[];
  addedEdges: Edge<LayerEdgeData>[];
  removedEdges: Edge<LayerEdgeData>[];
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
};

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

  return { addedNodes, removedNodes, addedEdges, removedEdges };
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
