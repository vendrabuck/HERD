import { cn } from "@/lib/cn";
import { LAYER_STYLES } from "../edges/layerStyles";
import type { EdgeLayerType } from "@/types/topology.types";

export interface LayerBadgeProps {
  layer: EdgeLayerType;
  onClick?: () => void;
  testId?: string;
  className?: string;
  // Overrides the plain layer color (issue #517 review round 3 item 6): the
  // review strip colors its badge from resolveEdgeStroke's status-aware
  // stroke (e.g. red for an uncabled pair) rather than the flat layer color,
  // so the pre-confirm signal survives being shown as a badge.
  strokeOverride?: string;
}

/**
 * The small layer pill (issue #517 review item 11): WiringDialog's
 * collapsed per-line pill (clickable, expands the layer control) and its
 * review-strip row badge (plain, non-interactive) share this.
 */
export function LayerBadge({ layer, onClick, testId, className, strokeOverride }: LayerBadgeProps) {
  const stroke = strokeOverride ?? LAYER_STYLES[layer].stroke;
  const sharedClassName = cn("text-[11px] font-bold px-1 py-0.5 rounded bg-white", className);
  const sharedStyle = { color: stroke, border: `1px solid ${stroke}` };

  if (onClick) {
    return (
      <button type="button" data-testid={testId} onClick={onClick} className={sharedClassName} style={sharedStyle}>
        {layer}
      </button>
    );
  }
  return (
    <span data-testid={testId} className={sharedClassName} style={sharedStyle}>
      {layer}
    </span>
  );
}
