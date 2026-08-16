import type { EdgeLayerType, LayerEdgeData } from "@/types/topology.types";
import { LAYER_STYLES } from "./layerStyles";

export const INVALID_STROKE = "#ef4444";
const VALID_PATH_STROKE = "#22c55e";

export interface EdgeStrokeInfo {
  stroke: string;
  // True when the edge fails validation (an uncabled port or an unreachable
  // device pair); the sole trigger for the shared red override.
  isInvalid: boolean;
  // Matches the inline label LayerEdge renders under the layer badge: null
  // when there is nothing to say yet (pathValid unresolved).
  statusLabel: string | null;
}

/**
 * Single source of truth for "is this edge valid, and what color/label does
 * that mean" (issue #517 review item 4b): LayerEdge, BundledEdge (which must
 * flip red the moment ANY bundled member is invalid, never silently hiding
 * the signal behind an averaged/neutral color), and the wiring dialog's
 * review rows all resolve status through this function instead of
 * duplicating the portsCabled/pathValid precedence.
 *
 * `data` is optional (review item 3): a topology loaded from an external or
 * legacy canvas_data can carry an edge with no `data` key at all despite the
 * app's own Edge<LayerEdgeData> typing assuming otherwise (@xyflow/react's
 * own EdgeBase types `data` as genuinely optional). Missing data defaults to
 * layer L2 exactly like LayerEdge's own `data?.layer ?? "L2"`, never throws.
 */
export function resolveEdgeStroke(data: LayerEdgeData | undefined): EdgeStrokeInfo {
  const layer: EdgeLayerType = data?.layer ?? "L2";
  const baseStroke = (LAYER_STYLES[layer] ?? LAYER_STYLES.L2).stroke;

  if (data?.portsCabled === false) {
    return { stroke: INVALID_STROKE, isInvalid: true, statusLabel: "uncabled port" };
  }
  if (data?.pathValid === true) {
    return {
      stroke: VALID_PATH_STROKE,
      isInvalid: false,
      statusLabel: `${data?.pathHopCount ?? 0} hops`,
    };
  }
  if (data?.pathValid === false) {
    return { stroke: INVALID_STROKE, isInvalid: true, statusLabel: "no path" };
  }
  return { stroke: baseStroke, isInvalid: false, statusLabel: null };
}
