import type { Node } from "@xyflow/react";
import type { Device } from "@/types/device.types";
import type { CanvasNodeData, DeviceNodeData, InvalidRoute, L3RouteIntent } from "@/types/topology.types";
import { canvasNodeLabel, isDeviceNode, l3RoutesOf } from "@/lib/canvasNodes";

// ADR 0014 phase 2 (issue #34) review fix F11: the one place that decides
// whether a device is a Layer 3 Switch, replacing a bare string-literal
// comparison at each call site.
export function isLayer3Switch(device: Device): boolean {
  return device.connection_type === "Layer 3 Switch";
}

// ADR 0014 phase 2 (issue #34), E5: the "<device name>[<index>] <reason>"
// label the plain-topology-save validate toast names the first three
// invalid_routes entries with. A switch-level entry (index null, e.g.
// l3_malformed, l3_not_a_router, l3_switch_unconfigured,
// l3_switch_unattached) renders its index as "-" rather than the string
// "null". Uses the shared `canvasNodeLabel` (review fix F11); falls back to
// the raw node_id if the node is no longer on the canvas (should not happen
// within one render, but a stale result from a prior canvas must not crash
// the toast).
export function routeProblemLabel(route: InvalidRoute, nodes: Node<CanvasNodeData>[]): string {
  const node = nodes.find((n) => n.id === route.node_id);
  const deviceName = canvasNodeLabel(node, route.node_id);
  const index = route.index === null ? "-" : String(route.index);
  return `${deviceName}[${index}] ${route.reason}`;
}

// ADR 0014 phase 2 (issue #34), E3: the Routing panel's gating rule, factored
// out of TopologyEditorPage.tsx (matching the canvasNodes.ts precedent) so it
// is importable and unit-testable without mounting the page. The panel
// renders only when exactly one device node is selected, it is not a
// proposal (ghost) node, and its device is a Layer 3 Switch. `isReadOnly` is
// the page's own union of an archived fork's as-built record and a
// fork-history preview/diff overlay, the same flag every other edit
// affordance on the canvas already gates on.
export function selectRoutingPanelNode(
  nodes: Node<CanvasNodeData>[],
  isReadOnly: boolean,
): Node<DeviceNodeData> | null {
  if (isReadOnly) return null;
  const selectedDeviceNodes = nodes.filter((n) => n.selected && isDeviceNode(n));
  if (selectedDeviceNodes.length !== 1) return null;
  const node = selectedDeviceNodes[0] as Node<DeviceNodeData>;
  if (node.data.isProposal) return null;
  if (!isLayer3Switch(node.data.device)) return null;
  return node;
}

// ADR 0014 phase 2 (issue #34) review fix F1: cabling's `route_causes_invalid`
// (services/cabling/app/services/l3_validation.py) excludes `l3_duplicate_route`
// from `valid` and from the save-gate's refusal: a duplicate route identity is
// informational (the set reconcile collapses it harmlessly), never itself a
// reason to block anything. Every problem COUNT (a toast's "N problems", the
// red badge) must use only entries this returns true for; a duplicate still
// renders in the Routing panel, just not as a blocking, red one.
export function isBlockingRouteProblem(problem: { reason: string }): boolean {
  return problem.reason !== "l3_duplicate_route";
}

// ADR 0014 phase 2 (issue #34) review fix F4: one L3 validation-result entry,
// resolved against the canvas that was ACTUALLY JUDGED at the moment the
// result arrived, rather than left as a raw index into whatever canvas the
// store happens to hold when it is later rendered. `route` is the full route
// VALUE at that index (or null for a switch-level entry, index null): once
// resolved, a caller matches it back onto CURRENT rows by field equality
// (destination, next_hop, interface, virtual_router), which survives a
// Remove reordering the array in a way a bare index never could.
export interface ResolvedRouteProblem {
  node_id: string;
  route: L3RouteIntent | null;
  reason: string;
  detail: string | null;
}

// Resolves a raw `InvalidRoute[]` (as cabling returns it) against `nodes`,
// which MUST be the canvas the result was judged against: `persistableCanvas.nodes`
// at save/fork-save time (the nodes that were actually sent), or the
// persisted `topology.canvas_data.nodes` for a reservation-create refusal
// (the gate validates the PERSISTED topology, not whatever the store
// currently holds). A node absent from `nodes`, or an index past the end of
// its routes, resolves to `route: null` for a per-route entry too (treated
// like an unmatched switch-level line by the panel, never a crash).
export function resolveRouteProblems(
  invalidRoutes: InvalidRoute[],
  nodes: Node<CanvasNodeData>[],
): ResolvedRouteProblem[] {
  const nodesById = new Map(nodes.map((n) => [n.id, n]));
  return invalidRoutes.map((r) => {
    if (r.index === null) {
      return { node_id: r.node_id, route: null, reason: r.reason, detail: r.detail };
    }
    const node = nodesById.get(r.node_id);
    const routes = node ? l3RoutesOf(node.data as DeviceNodeData) : [];
    const route = routes[r.index] ?? null;
    return { node_id: r.node_id, route, reason: r.reason, detail: r.detail };
  });
}

function routeFieldsEqual(a: L3RouteIntent, b: L3RouteIntent): boolean {
  return (
    a.destination === b.destination &&
    a.interface === b.interface &&
    a.next_hop === b.next_hop &&
    a.virtual_router === b.virtual_router
  );
}

export interface MatchedRouteProblems {
  // Switch-level entries (route: null, whether genuinely switch-level or an
  // out-of-range index) render at the top of the panel.
  switchLevel: ResolvedRouteProblem[];
  // Per-row entries, keyed by the row's CURRENT index in `routes`.
  perRow: Map<number, ResolvedRouteProblem[]>;
}

// ADR 0014 phase 2 (issue #34) review fix F4: matches each resolved problem
// back onto the CURRENT rows by field equality, not by its original index.
// "First unmatched row wins": each already-matched index is excluded from
// matching a later problem in the same call, so two rows sharing identical
// fields (a not-yet-saved duplicate) each still get their own line rather
// than both problems piling onto the first one. A problem whose route no
// longer matches any current row (the row was edited or removed since the
// canvas was judged) is silently dropped: the panel has nothing correct left
// to attach it to, and the node's badge staying red until the next result is
// the intended signal that something here may still be wrong.
// The route field each validation reason is ABOUT (ADR 0014 addendum X-I,
// issue #755). The Routing panel uses this to outline the offending input, so a
// reason like `l3_unknown_virtual_router` points at the Virtual router box
// rather than leaving the user to map a raw reason string onto a column
// themselves. A reason that is not about one field (every switch-level reason,
// and the informational `l3_duplicate_route`) returns null and highlights
// nothing.
//
// The three VRF reasons are the ones X-I added, and their mapping is the part
// worth stating twice: `l3_unknown_virtual_router` and
// `l3_interface_outside_virtual_router` are both about the VRF the route names,
// so they point at Virtual router; `l3_interface_bound_to_virtual_router` fires
// on a route that names NO VRF, so its problem is the interface (it is enslaved
// to a VRF), and it points at Interface.
export type RouteProblemField = "destination" | "next_hop" | "interface" | "virtual_router";

const ROUTE_PROBLEM_FIELDS: Record<string, RouteProblemField> = {
  l3_bad_destination: "destination",
  l3_bad_next_hop: "next_hop",
  l3_next_hop_unverifiable: "next_hop",
  l3_next_hop_outside_interface: "next_hop",
  l3_unknown_interface: "interface",
  l3_interface_bound_to_virtual_router: "interface",
  l3_unknown_virtual_router: "virtual_router",
  l3_interface_outside_virtual_router: "virtual_router",
};

export function routeProblemField(reason: string): RouteProblemField | null {
  return ROUTE_PROBLEM_FIELDS[reason] ?? null;
}

// The set of fields to outline for one row, given every problem attached to it.
// Only blocking problems contribute: an informational duplicate must not paint
// a field red.
export function routeProblemFields(problems: { reason: string }[]): Set<RouteProblemField> {
  const fields = new Set<RouteProblemField>();
  for (const problem of problems) {
    if (!isBlockingRouteProblem(problem)) continue;
    const field = routeProblemField(problem.reason);
    if (field) fields.add(field);
  }
  return fields;
}

export function matchRouteProblems(
  routes: L3RouteIntent[],
  problems: ResolvedRouteProblem[],
): MatchedRouteProblems {
  const used = new Set<number>();
  const perRow = new Map<number, ResolvedRouteProblem[]>();
  const switchLevel: ResolvedRouteProblem[] = [];
  for (const problem of problems) {
    if (problem.route === null) {
      switchLevel.push(problem);
      continue;
    }
    let matchedIndex = -1;
    for (let i = 0; i < routes.length; i++) {
      if (used.has(i)) continue;
      if (routeFieldsEqual(routes[i], problem.route)) {
        matchedIndex = i;
        break;
      }
    }
    if (matchedIndex === -1) continue;
    used.add(matchedIndex);
    const existing = perRow.get(matchedIndex);
    if (existing) {
      existing.push(problem);
    } else {
      perRow.set(matchedIndex, [problem]);
    }
  }
  return { switchLevel, perRow };
}
