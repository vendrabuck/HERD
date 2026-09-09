import type { Edge, Node } from "@xyflow/react";
import type {
  CanvasData,
  CanvasNodeData,
  DeviceNodeData,
  L3RouteIntent,
  LayerEdgeData,
} from "@/types/topology.types";
import { isDeviceNode } from "@/lib/canvasNodes";

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

// --- ADR 0014 phase 2 peer-review addition (issue #34): destination
// canonicalization, mirroring the server's exactly. -----------------------
//
// Cabling's l3_intent.py canonicalizes a route's `destination` at save time
// via Python's `str(ipaddress.ip_network(value, strict=False))`: a host-form
// CIDR like "10.0.0.1/24" is stored as "10.0.0.0/24" (host bits masked off),
// a bare address gets an implicit host prefix ("10.0.0.1" -> "10.0.0.1/32",
// an IPv6 address -> "/128"), and IPv6 renders lowercase in RFC 5952
// compressed form. The CANVAS keeps whatever the user typed (the Routing
// panel never rewrites it, ADR 0014 Decision 5's "the server is the sole
// authority" extends to not second-guessing displayed text), so a version
// saved before/after this canonicalization can show the "same" route as two
// different strings. The fork diff must compare canonicalized destinations,
// or a version-to-version diff would report a routing change nobody made.
// An unparseable string (which the server rejects as l3_bad_destination
// regardless) is kept verbatim, matching the server's own fallback.
function parseIPv4Octets(value: string): number[] | null {
  const parts = value.split(".");
  if (parts.length !== 4) return null;
  const octets: number[] = [];
  for (const part of parts) {
    if (!/^(0|[1-9]\d{0,2})$/.test(part)) return null;
    const n = Number(part);
    if (n > 255) return null;
    octets.push(n);
  }
  return octets;
}

function canonicalizeIPv4(addressPart: string, prefixPart: string | undefined): string | null {
  const octets = parseIPv4Octets(addressPart);
  if (!octets) return null;
  let prefixLen = 32;
  if (prefixPart !== undefined) {
    if (!/^\d{1,2}$/.test(prefixPart)) return null;
    prefixLen = Number(prefixPart);
    if (prefixLen > 32) return null;
  }
  const addrInt =
    ((octets[0] << 24) | (octets[1] << 16) | (octets[2] << 8) | octets[3]) >>> 0;
  const mask = prefixLen === 0 ? 0 : (0xffffffff << (32 - prefixLen)) >>> 0;
  const network = (addrInt & mask) >>> 0;
  const networkOctets = [
    (network >>> 24) & 255,
    (network >>> 16) & 255,
    (network >>> 8) & 255,
    network & 255,
  ];
  return `${networkOctets.join(".")}/${prefixLen}`;
}

// Expands "::" and validates a (possibly IPv4-mapped-tail) IPv6 address
// string into its 8 16-bit groups.
function parseIPv6Groups(address: string): number[] | null {
  let addr = address;
  if (addr.includes(".")) {
    // An embedded IPv4 tail (e.g. "::ffff:192.0.2.1"): fold its last 32
    // bits into two hextets before the normal colon-group parse below.
    const lastColon = addr.lastIndexOf(":");
    if (lastColon === -1) return null;
    const v4 = parseIPv4Octets(addr.slice(lastColon + 1));
    if (!v4) return null;
    const hex1 = ((v4[0] << 8) | v4[1]).toString(16);
    const hex2 = ((v4[2] << 8) | v4[3]).toString(16);
    addr = `${addr.slice(0, lastColon + 1)}${hex1}:${hex2}`;
  }
  const doubleColonCount = (addr.match(/::/g) ?? []).length;
  if (doubleColonCount > 1) return null;
  let head: string[];
  let tail: string[];
  if (addr.includes("::")) {
    const sides = addr.split("::");
    if (sides.length !== 2) return null;
    head = sides[0].length > 0 ? sides[0].split(":") : [];
    tail = sides[1].length > 0 ? sides[1].split(":") : [];
  } else {
    head = addr.split(":");
    tail = [];
  }
  const missing = 8 - (head.length + tail.length);
  if (!addr.includes("::") && missing !== 0) return null;
  if (addr.includes("::") && missing < 0) return null;
  const zeros = addr.includes("::") ? Array<string>(missing).fill("0") : [];
  const allParts = [...head, ...zeros, ...tail];
  if (allParts.length !== 8) return null;
  const groups: number[] = [];
  for (const part of allParts) {
    if (!/^[0-9a-fA-F]{1,4}$/.test(part)) return null;
    groups.push(parseInt(part, 16));
  }
  return groups;
}

// RFC 5952 canonical compressed lowercase form: the longest run of two-or-
// more zero groups collapses to "::" (leftmost run wins a length tie).
function groupsToCanonicalIPv6(groups: number[]): string {
  let bestStart = -1;
  let bestLen = 0;
  let curStart = -1;
  let curLen = 0;
  for (let i = 0; i < groups.length; i++) {
    if (groups[i] === 0) {
      if (curStart === -1) curStart = i;
      curLen++;
      if (curLen > bestLen) {
        bestLen = curLen;
        bestStart = curStart;
      }
    } else {
      curStart = -1;
      curLen = 0;
    }
  }
  const hexParts = groups.map((g) => g.toString(16));
  if (bestLen >= 2) {
    const before = hexParts.slice(0, bestStart);
    const after = hexParts.slice(bestStart + bestLen);
    if (before.length === 0 && after.length === 0) return "::";
    if (before.length === 0) return `::${after.join(":")}`;
    if (after.length === 0) return `${before.join(":")}::`;
    return `${before.join(":")}::${after.join(":")}`;
  }
  return hexParts.join(":");
}

function canonicalizeIPv6(addressPart: string, prefixPart: string | undefined): string | null {
  const groups = parseIPv6Groups(addressPart);
  if (!groups) return null;
  let prefixLen = 128;
  if (prefixPart !== undefined) {
    if (!/^\d{1,3}$/.test(prefixPart)) return null;
    prefixLen = Number(prefixPart);
    if (prefixLen > 128) return null;
  }
  const masked = groups.map((group, i) => {
    const groupStart = i * 16;
    const groupEnd = groupStart + 16;
    if (groupEnd <= prefixLen) return group;
    if (groupStart >= prefixLen) return 0;
    const bitsToKeep = prefixLen - groupStart;
    const mask = (0xffff << (16 - bitsToKeep)) & 0xffff;
    return group & mask;
  });
  return `${groupsToCanonicalIPv6(masked)}/${prefixLen}`;
}

/**
 * Mirrors cabling's `str(ipaddress.ip_network(value, strict=False))` for the
 * common IPv4/IPv6 cases (with or without a prefix). Returns the input
 * unchanged when it does not parse as either (the server's own fallback for
 * `l3_bad_destination`, which the fork diff must not paper over as "no
 * change" either way, since two differently-malformed strings are, in fact,
 * different).
 */
export function canonicalizeDestination(value: string): string {
  const trimmed = value.trim();
  if (trimmed.length === 0) return value;
  const slashIndex = trimmed.indexOf("/");
  const addressPart = slashIndex === -1 ? trimmed : trimmed.slice(0, slashIndex);
  const prefixPart = slashIndex === -1 ? undefined : trimmed.slice(slashIndex + 1);
  const canonical = addressPart.includes(":")
    ? canonicalizeIPv6(addressPart, prefixPart)
    : canonicalizeIPv4(addressPart, prefixPart);
  return canonical ?? value;
}

// The route-set identity key the fork diff compares on (E6): order-
// insensitive across (canonicalized destination, interface, next_hop), plus
// virtual_router, matching the brief's stated identity fields.
function routeSetDiffKey(route: L3RouteIntent): string {
  return [
    canonicalizeDestination(route.destination),
    route.interface,
    route.next_hop ?? "",
    route.virtual_router ?? "",
  ].join("|");
}

function l3Routes(node: Node<CanvasNodeData> | undefined): L3RouteIntent[] {
  if (!node || !isDeviceNode(node)) return [];
  return (node.data as DeviceNodeData).l3?.routes ?? [];
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
