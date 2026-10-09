import type { Edge } from "@xyflow/react";

import type { LayerEdgeData } from "@/types/topology.types";

/**
 * Server-reported invalid canvas lines (issue #1066).
 *
 * The editor's own checks judge a line by its device pair (pathfind) and by
 * whether each chosen port has some cable (`portsCabled`). A line between two
 * cabled ports that no cable joins to each other passes both, yet the fork
 * save builds nothing for it (issue #1007; no fallback to other ports, issue
 * #531). Cabling's edge validator reports exactly that case as `no_port_path`
 * (issue #1047), and the fork's loose canvas PUT returns its answer as
 * `invalid_edges`. This module turns that answer into a map from canvas edge
 * id to reason, which the topology editor overlays onto its render-only edge
 * view so such a line paints red with the reason, the same red the client
 * checks paint.
 *
 * The map is page state, never canvas data: `withServerEdgeProblems` writes
 * `serverInvalidReason` onto COPIES of the edges React Flow renders, and the
 * persist path strips the field as well, so it can never reach `canvas_data`.
 */
export type ServerEdgeProblems = ReadonlyMap<string, string>;

export const EMPTY_SERVER_EDGE_PROBLEMS: ServerEdgeProblems = new Map();

/** The reason cabling's validator and the fork save use for a line on unjoined ports. */
export const NO_PORT_PATH_REASON = "no_port_path";

// Plain-words rendering of cabling's InvalidEdge.reason vocabulary
// (services/cabling/app/schemas/topology.py). Shared by the AI commit dialog's
// refusal list and the editor's edge labels; a reason not listed here is shown
// as the raw string rather than hidden.
const INVALID_EDGE_REASON_TEXT: Record<string, string> = {
  no_path: "no cable path",
  [NO_PORT_PATH_REASON]: "no cable path on the chosen ports",
  missing_device: "device not found",
  element_to_element: "cannot connect two elements directly",
  element_edge_no_port: "element has no available port",
};

export function invalidEdgeReasonText(reason: string): string {
  return INVALID_EDGE_REASON_TEXT[reason] ?? reason;
}

/**
 * Narrows a canvas PUT's `invalid_edges` to edge id to reason. Entries without
 * a non-empty string `edge_id` and a non-empty string `reason` are skipped
 * (they cannot be placed on a line); the first reason per edge id wins. Any
 * value that is not an array yields an empty map.
 */
export function serverEdgeProblemsFrom(invalidEdges: unknown): ServerEdgeProblems {
  if (!Array.isArray(invalidEdges)) return EMPTY_SERVER_EDGE_PROBLEMS;
  const problems = new Map<string, string>();
  for (const entry of invalidEdges) {
    if (!entry || typeof entry !== "object") continue;
    const { edge_id: edgeId, reason } = entry as { edge_id?: unknown; reason?: unknown };
    if (typeof edgeId !== "string" || !edgeId) continue;
    if (typeof reason !== "string" || !reason) continue;
    if (!problems.has(edgeId)) problems.set(edgeId, reason);
  }
  return problems.size === 0 ? EMPTY_SERVER_EDGE_PROBLEMS : problems;
}

/**
 * Folds a fork save's `constrained_edges_skipped` into the map. The save is a
 * fresh judgment of the committed canvas on exactly one reason, `no_port_path`
 * (the save and the validator share one port-constrained search, issue
 * #1047), so every `no_port_path` entry is replaced by the save's list; other
 * reasons came from the last canvas PUT, which the save does not re-judge, and
 * are kept. A skipped entry with no edge id cannot be placed and is ignored.
 */
export function applySkippedConstrainedEdges(
  current: ServerEdgeProblems,
  skipped: ReadonlyArray<{ edge_id?: string | null }> | undefined,
): ServerEdgeProblems {
  const next = new Map<string, string>();
  for (const [edgeId, reason] of current) {
    if (reason !== NO_PORT_PATH_REASON) next.set(edgeId, reason);
  }
  for (const entry of skipped ?? []) {
    if (typeof entry.edge_id === "string" && entry.edge_id) {
      next.set(entry.edge_id, NO_PORT_PATH_REASON);
    }
  }
  return next.size === 0 ? EMPTY_SERVER_EDGE_PROBLEMS : next;
}

/**
 * The render view of `edges` with each server-reported line carrying its
 * reason in `data.serverInvalidReason`. Returns the SAME array when nothing
 * matches, so a memo built on it stays stable; matched edges are shallow
 * copies and the input is never mutated.
 */
export function withServerEdgeProblems(
  edges: Edge<LayerEdgeData>[],
  problems: ServerEdgeProblems,
): Edge<LayerEdgeData>[] {
  if (problems.size === 0) return edges;
  let changed = false;
  const result = edges.map((edge) => {
    const reason = problems.get(edge.id);
    if (!reason) return edge;
    changed = true;
    const data = { ...(edge.data ?? { layer: "L2" as const }), serverInvalidReason: reason };
    return { ...edge, data };
  });
  return changed ? result : edges;
}

/** Removes the render-only `serverInvalidReason` from an edge's data, if present. */
export function stripServerEdgeProblem(edge: Edge<LayerEdgeData>): Edge<LayerEdgeData> {
  if (!edge.data || !("serverInvalidReason" in edge.data)) return edge;
  const { serverInvalidReason: _reason, ...data } = edge.data;
  return { ...edge, data: data as LayerEdgeData };
}
