import type { Node } from "@xyflow/react";
import type { CanvasNodeData, DeviceNodeData } from "@/types/topology.types";

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
