import {
  BaseEdge,
  EdgeLabelRenderer,
  getBezierPath,
  type EdgeProps,
} from "@xyflow/react";
import type { LayerEdge as LayerEdgeType } from "@/types/topology.types";
import { LAYER_STYLES } from "./layerStyles";
import { resolveEdgeStroke } from "./edgeStatus";

export function LayerEdge({
  id,
  sourceX,
  sourceY,
  targetX,
  targetY,
  sourcePosition,
  targetPosition,
  data,
}: EdgeProps<LayerEdgeType>) {
  const layer = data?.layer ?? "L2";
  const baseStyle = LAYER_STYLES[layer] ?? LAYER_STYLES.L2;
  const isProposal = data?.isProposal === true;

  // Physical reachability is the only authority; applies at every layer
  // because L2/L3 still ride on the L1 cable graph. resolveEdgeStroke is the
  // single source of truth for this precedence (stroke color AND the status
  // label), shared with BundledEdge; passing `data` straight through also
  // covers a data-less edge (review item 3) the same way resolveEdgeStroke's
  // own default does, with no separate null-check needed here.
  const { stroke, statusLabel } = resolveEdgeStroke(data);
  // Proposals always render dashed in the proposal-edge color so they are
  // visually distinct from committed edges even when the same L1/L2/L3 style
  // would otherwise apply.
  const strokeDasharray = isProposal ? "4 3" : baseStyle.strokeDasharray;
  const style = { ...baseStyle, stroke, strokeDasharray };
  const edgeOpacity = isProposal ? 0.6 : 1;

  const sourceName = data?.source_port_name;
  const targetName = data?.target_port_name;
  let portLabel: string | null = null;
  if (sourceName && targetName) {
    portLabel = `${sourceName} - ${targetName}`;
  } else if (sourceName) {
    portLabel = sourceName;
  } else if (targetName) {
    portLabel = targetName;
  }

  const [edgePath, labelX, labelY] = getBezierPath({
    sourceX,
    sourceY,
    sourcePosition,
    targetX,
    targetY,
    targetPosition,
  });

  return (
    <>
      <BaseEdge
        id={id}
        path={edgePath}
        style={{
          stroke: style.stroke,
          strokeWidth: 3,
          strokeDasharray: style.strokeDasharray,
          opacity: edgeOpacity,
        }}
      />
      <EdgeLabelRenderer>
        <div
          style={{
            position: "absolute",
            transform: `translate(-50%, -50%) translate(${labelX}px,${labelY}px)`,
            pointerEvents: "all",
          }}
          className="nodrag nopan flex flex-col items-center gap-0.5"
        >
          <span
            className="text-xs font-bold px-1 py-0.5 rounded"
            style={{ color: style.stroke, backgroundColor: "white", border: `1px solid ${style.stroke}` }}
          >
            {style.label}
          </span>
          {portLabel && (
            <span
              className="text-xs px-1 py-0.5 rounded whitespace-nowrap"
              style={{ color: style.stroke, backgroundColor: "white", border: `1px solid ${style.stroke}` }}
            >
              {portLabel}
            </span>
          )}
          {statusLabel && (
            <span
              className="text-xs px-1 py-0.5 rounded whitespace-nowrap"
              style={{ color: stroke, backgroundColor: "white", border: `1px solid ${stroke}` }}
            >
              {statusLabel}
            </span>
          )}
        </div>
      </EdgeLabelRenderer>
    </>
  );
}
