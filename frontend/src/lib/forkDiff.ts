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

  const beforeEdgeKeys = new Set(beforeEdges.map(edgeIdentityKey));
  const afterEdgeKeys = new Set(afterEdges.map(edgeIdentityKey));
  const addedEdges = afterEdges.filter((e) => !beforeEdgeKeys.has(edgeIdentityKey(e)));
  const removedEdges = beforeEdges.filter((e) => !afterEdgeKeys.has(edgeIdentityKey(e)));

  return { addedNodes, removedNodes, addedEdges, removedEdges };
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
