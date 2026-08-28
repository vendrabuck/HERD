import type { Edge } from "@xyflow/react";
import type { LayerEdgeData } from "@/types/topology.types";

/**
 * True for an edge that is a read-only annotation rather than real committed
 * wiring: an AI ghost proposal (`isProposal`) or a fork version diff overlay
 * edge (`diffStatus`, issue #622). Shared by groupEdgesForRender's own
 * bundling exclusion below and by TopologyEditorPage's pathfind/validity
 * logic, so a diff overlay edge (which carries `diffStatus` but not
 * `isProposal`) is excluded everywhere a proposal edge already was, not just
 * here.
 */
export function isAnnotationEdge(data: LayerEdgeData | undefined): boolean {
  return !!data?.isProposal || !!data?.diffStatus;
}

export interface BundledEdgeData extends Record<string, unknown> {
  // A member's data can be missing at runtime even though the app's own
  // typing assumes otherwise (review item 3): an externally- or legacy-
  // sourced canvas_data can carry an edge with no `data` key.
  members: Array<{ id: string; data: LayerEdgeData | undefined }>;
  // Threaded through so BundledEdge can hide its per-member delete control
  // in read-only mode (issue #517 review round 3 item 3): that control calls
  // the store directly rather than going through React Flow's own change
  // stream, so it does not automatically inherit the ReactFlow component's
  // elementsSelectable/deleteKeyCode read-only gating the way the
  // bundle-level (Delete key) path does. An archived fork's as-built canvas
  // must not be editable through this side door.
  isReadOnly: boolean;
}

export type RenderEdge = Edge<LayerEdgeData> | Edge<BundledEdgeData>;

export interface GroupEdgesResult {
  renderEdges: RenderEdge[];
  // Bundle render-id to its member store-edge ids (issue #517 review item
  // 3): React Flow only ever sees the synthetic bundle id for a grouped
  // pair, so any change targeting it (selection, Delete) has to be expanded
  // back to every member id before it reaches the store, or a bundle is
  // permanently unselectable and undeletable.
  bundleMembers: Map<string, string[]>;
}

/**
 * Render-only grouping (ADR: canvas store always holds N distinct edges).
 * Two or more edges sharing an unordered device pair collapse into one
 * synthetic bundledEdge for the React Flow `edges` prop; a lone edge between
 * a pair passes through unchanged as today's layerEdge. This function never
 * mutates or drops the underlying store edges: it only changes what gets
 * handed to <ReactFlow edges=.../> for painting.
 *
 * The synthetic bundle edge carries the FIRST member's sourceHandle/
 * targetHandle (issue #517 review item 4a) so it anchors where the
 * connections were actually drawn; under ConnectionMode.Loose an edge with
 * no handle falls back to a default anchor (visually top-to-top), which
 * reads as wrong once real per-line handles are in play.
 *
 * The bundle also projects `selected`/`animated`/`style`/`zIndex` from its
 * members (issue #517 review item 1, root cause of the original item 3 bug):
 * React Flow is a CONTROLLED component for edges, so it reconciles its own
 * internal selection/highlight state to whatever the `edges` prop says on
 * every render. A freshly-built bundle object that never echoes `selected`
 * back reads as permanently unselected to React Flow no matter what
 * happened to its members, which made a bundle unselectable and undeletable
 * (RF never even emits the follow-up deselect, since as far as it can tell
 * nothing was ever selected) and, worse, let handleEdgesChange's select
 * expansion stamp `selected: true` onto the real store edges with no way for
 * RF to ever ask for it to be cleared again, which then leaked into
 * persisted canvas_data. `selected`/`animated` OR across members (any member
 * selected/animated marks the whole bundle so); `style`/`zIndex` take the
 * first member's, since those are not meaningfully per-member here.
 *
 * Proposal edges (AI ghost edges) are excluded from bundling: they are not
 * real committed wiring and should keep rendering individually. A fork
 * version diff overlay edge (issue #622, `data.diffStatus` set) is excluded
 * for the same reason: it is a read-only annotation, not a real duplicate
 * wire, and bundling it would hide its added/removed color under a plain
 * count badge.
 */
export function groupEdgesForRender(
  edges: Edge<LayerEdgeData>[],
  isReadOnly = false,
): GroupEdgesResult {
  const groups = new Map<string, Edge<LayerEdgeData>[]>();
  const order: string[] = [];

  for (const edge of edges) {
    if (isAnnotationEdge(edge.data)) continue;
    const pairKey = [edge.source, edge.target].sort().join("::");
    if (!groups.has(pairKey)) {
      groups.set(pairKey, []);
      order.push(pairKey);
    }
    groups.get(pairKey)!.push(edge);
  }

  const proposals = edges.filter((e) => isAnnotationEdge(e.data));
  const renderEdges: RenderEdge[] = [];
  const bundleMembers = new Map<string, string[]>();

  for (const key of order) {
    const members = groups.get(key)!;
    if (members.length === 1) {
      renderEdges.push(members[0]);
      continue;
    }
    const first = members[0];
    const bundleId = `bundle-${key}`;
    bundleMembers.set(bundleId, members.map((m) => m.id));
    renderEdges.push({
      id: bundleId,
      source: first.source,
      target: first.target,
      sourceHandle: first.sourceHandle,
      targetHandle: first.targetHandle,
      type: "bundledEdge",
      selected: members.some((m) => m.selected),
      animated: members.some((m) => m.animated),
      style: first.style,
      zIndex: first.zIndex,
      data: {
        members: members.map((m) => ({ id: m.id, data: m.data })),
        isReadOnly,
      },
    });
  }

  return { renderEdges: [...renderEdges, ...proposals], bundleMembers };
}
