import type { Node } from "@xyflow/react";
import type { CanvasNodeData, DeviceNodeData, InvalidRoute } from "@/types/topology.types";
import { isDeviceNode } from "@/lib/canvasNodes";

// ADR 0014 phase 2 (issue #34), E5: the "<device name>[<index>] <reason>"
// label the plain-topology-save validate toast names the first three
// invalid_routes entries with. A switch-level entry (index null, e.g.
// l3_malformed, l3_not_a_router, l3_switch_unconfigured,
// l3_switch_unattached) renders its index as "-" rather than the string
// "null". Falls back to the raw node_id if the node is no longer on the
// canvas (should not happen within one render, but a stale result from a
// prior canvas must not crash the toast).
export function routeProblemLabel(route: InvalidRoute, nodes: Node<CanvasNodeData>[]): string {
  const node = nodes.find((n) => n.id === route.node_id);
  const deviceName =
    node && isDeviceNode(node) ? (node.data as DeviceNodeData).device.name : route.node_id;
  const index = route.index === null ? "-" : String(route.index);
  return `${deviceName}[${index}] ${route.reason}`;
}

// ADR 0014 phase 2 (issue #34), E3: the Routing panel's gating rule, factored
// out of TopologyEditorPage.tsx (matching the canvasNodes.ts precedent) so it
// is importable and unit-testable without mounting the page. The panel
// renders only when exactly one device node is selected, it is not a
// proposal (ghost) node, and its device's connection_type is exactly
// "Layer 3 Switch". `isReadOnly` is the page's own union of an archived
// fork's as-built record and a fork-history preview/diff overlay, the same
// flag every other edit affordance on the canvas already gates on.
export function selectRoutingPanelNode(
  nodes: Node<CanvasNodeData>[],
  isReadOnly: boolean,
): Node<DeviceNodeData> | null {
  if (isReadOnly) return null;
  const selectedDeviceNodes = nodes.filter((n) => n.selected && isDeviceNode(n));
  if (selectedDeviceNodes.length !== 1) return null;
  const node = selectedDeviceNodes[0] as Node<DeviceNodeData>;
  if (node.data.isProposal) return null;
  if (node.data.device.connection_type !== "Layer 3 Switch") return null;
  return node;
}
