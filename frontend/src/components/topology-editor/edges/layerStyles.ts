import type { EdgeLayerType } from "@/types/topology.types";

export interface LayerStyle {
  stroke: string;
  strokeDasharray?: string;
  label: string;
}

// L1 gray dashed, L2 blue solid, L3 green dashed. Single source of truth for
// the layer encoding shared by LayerEdge (canvas), the wiring dialog's own
// lines and per-line layer pills, and the port-column connection dots.
export const LAYER_STYLES: Record<EdgeLayerType, LayerStyle> = {
  L1: { stroke: "#9ca3af", strokeDasharray: "6 3", label: "L1" },
  L2: { stroke: "#3b82f6", label: "L2" },
  L3: { stroke: "#22c55e", strokeDasharray: "3 3", label: "L3" },
};

// Segmented-control fill classes for the active layer, matching
// ConnectionModal's original LAYER_COLORS.
export const LAYER_SEGMENT_CLASSES: Record<EdgeLayerType, string> = {
  L1: "bg-gray-500 text-white",
  L2: "bg-blue-500 text-white",
  L3: "bg-green-500 text-white",
};

// Ordered layer options, derived from LAYER_STYLES so a new layer only needs
// adding there. Shared by every L1/L2/L3 segmented control in the editor
// (WiringDialog, QuickConnectPopover, TopologyEditorPage's toolbar).
export const LAYER_OPTIONS = Object.keys(LAYER_STYLES) as EdgeLayerType[];
