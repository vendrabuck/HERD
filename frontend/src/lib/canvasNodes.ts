import type { Node } from "@xyflow/react";
import type { CanvasNodeData, DeviceNodeData, L3RouteIntent } from "@/types/topology.types";

// Canvas node type predicates, factored out of TopologyEditorPage.tsx (review
// fix) so collectCanvasDeviceIds is importable and unit-testable without
// pulling in the page component, and so react-refresh's
// only-export-components rule stays satisfied for the page file (a page
// module may export only components).

// Placeholder nodes are canvas-local planning artifacts: no inventory device
// id, no cabling, never persisted as devices or wiring.
export const isDynamicPlaceholder = (node: Node<CanvasNodeData>) =>
  node.type === "dynamicPlaceholderNode";

// Network element nodes are the OPPOSITE of placeholders in one crucial way
// (ADR 0012 "Canvas shape"): they DO persist into canvas_data.
export const isNetworkElement = (node: Node<CanvasNodeData>) => node.type === "networkElementNode";

// Positive check for a real device node, i.e. one whose `data` is safe to
// cast to DeviceNodeData and read `.device` from. Prefer this over negating
// isDynamicPlaceholder/isNetworkElement at a `.device` read site: a negated
// pair silently stops being exhaustive if a fourth node type is ever added,
// while this fails closed (review fix: a networkElementNode reaching a
// `.device.id` read via an incomplete negation crashed handleAIProposal).
export const isDeviceNode = (node: Node<CanvasNodeData>) => node.type === "deviceNode";

// Pure helper (unit-testable without mounting the page): the set of
// inventory device ids for every real device node already on the canvas,
// excluding AI proposal ghosts. Used by handleAIProposal to skip any
// resolved device the resolver picked that is already on the canvas.
export function collectCanvasDeviceIds(nodes: Node<CanvasNodeData>[]): Set<string> {
  return new Set(
    nodes
      .filter(isDeviceNode)
      .filter((n) => !(n.data as DeviceNodeData).isProposal)
      .map((n) => (n.data as DeviceNodeData).device.id)
  );
}

// The shared fallback for l3RoutesOf below: a SINGLE stable reference, never
// a fresh `[]` literal per call. RoutingPanel reads `l3RoutesOf(node.data)`
// directly into a plain `const routes = ...` (not memoized) and feeds it
// into a `useEffect(..., [routes])` dependency array; a fresh array literal
// on every call is a different reference on every render even when nothing
// about the node's routing intent changed, which fails that dependency
// check on every render and re-fires the effect's setState calls forever (a
// live-hang, reproduced against this exact file: the empty/absent-l3 case,
// the MOST common one, spun every RoutingPanel render into an infinite
// render loop). A frozen module-level constant fixes it at the one place
// every caller shares, rather than pushing a useMemo onto each caller.
const EMPTY_L3_ROUTES = Object.freeze([] as L3RouteIntent[]) as L3RouteIntent[];

// ADR 0014 phase 2 (issue #34) review fix F6: the ONE safe accessor for a
// device node's routing intent. A persisted or externally-written canvas can
// carry a malformed `data.l3` (`{}`, `{"routes": null}`; the server accepts
// and stores these on a plain topology PUT, it only refuses them at fork
// save/create as `l3_intent_malformed`), and every prior direct read
// (`l3?.routes.length`, `l3?.routes ?? []`) threw on that shape, which the
// ErrorBoundary turned into a blanked, unrecoverable editor. Returns the
// shared `EMPTY_L3_ROUTES` for absent, malformed, AND valid-empty `data.l3`
// alike (never a fresh array); use `l3IsMalformed` below to tell a
// genuinely malformed shape apart from the other two when that distinction
// matters (the Routing panel's own repair affordance).
export function l3RoutesOf(data: DeviceNodeData | undefined): L3RouteIntent[] {
  const routes = data?.l3?.routes;
  return Array.isArray(routes) ? routes : EMPTY_L3_ROUTES;
}

// True only when `data.l3` is PRESENT but its `routes` key is not an array
// (the shape the server calls `l3_malformed`). False for both an absent
// `data.l3` (no intent) and a present-but-empty `{routes: []}` (R10: empty
// intent is no intent) alike, so a caller can render a distinct "this is
// broken, fix it" affordance only for the genuinely broken case.
export function l3IsMalformed(data: DeviceNodeData | undefined): boolean {
  if (!data || data.l3 === undefined) return false;
  return !Array.isArray(data.l3.routes);
}

// Shared device-node display label (review fix F11, issue #34): label, then
// device name, then the node id itself, in that order. Previously
// duplicated between `lib/l3.ts`'s `routeProblemLabel` and
// `ForkHistoryPanel.tsx`'s own `nodeLabel`. `fallbackId` is passed
// separately rather than read off `node.id` so a caller resolving a node by
// id that turns out to be absent from the canvas (a stale result naming a
// node no longer there) can still fall back to that id.
export function canvasNodeLabel(
  node: { data?: unknown } | undefined,
  fallbackId: string,
): string {
  const data = node?.data as DeviceNodeData | undefined;
  return data?.label || data?.device?.name || fallbackId;
}

// ADR 0014 phase 2 (issue #34), E5: true when any device node on the canvas
// carries at least one route. Decides whether a plain topology save's
// proactive `validateTopology` call (and the inventory round trips it makes
// server-side) is worth running at all, mirroring cabling's own
// `l3_intent.canvas_has_l3` gate. Built on `l3RoutesOf` (review fix F6), so
// a malformed `data.l3` counts as no intent here (the Routing panel surfaces
// and repairs that shape directly instead; see `l3IsMalformed`), matching
// the backend's R10 "empty intent is no intent" rule for the valid-empty case.
export function canvasHasL3Intent(nodes: Node<CanvasNodeData>[]): boolean {
  return nodes.some((n) => isDeviceNode(n) && l3RoutesOf(n.data as DeviceNodeData).length > 0);
}
