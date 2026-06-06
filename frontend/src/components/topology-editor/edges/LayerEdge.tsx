import {
  BaseEdge,
  EdgeLabelRenderer,
  getBezierPath,
  type EdgeProps,
} from "@xyflow/react";
import type { LayerEdge as LayerEdgeType } from "@/types/topology.types";

const LAYER_STYLES: Record<
  string,
  { stroke: string; strokeDasharray?: string; label: string }
> = {
  L1: { stroke: "#9ca3af", strokeDasharray: "6 3", label: "L1" },
  L2: { stroke: "#3b82f6", label: "L2" },
  L3: { stroke: "#22c55e", strokeDasharray: "3 3", label: "L3" },
};

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
  const pathValid = data?.pathValid;
  const portsCabled = data?.portsCabled;
  const isProposal = data?.isProposal === true;

  // Override stroke color for uncabled ports or unreachable device pairs.
  // Physical reachability is the only authority; applies at every layer because
  // L2/L3 still ride on the L1 cable graph.
  let stroke = baseStyle.stroke;
  if (portsCabled === false) {
    stroke = "#ef4444";
  } else if (pathValid === true) {
    stroke = "#22c55e";
  } else if (pathValid === false) {
    stroke = "#ef4444";
  }
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
          {portsCabled === false && (
            <span
              className="text-xs px-1 py-0.5 rounded whitespace-nowrap"
              style={{ color: "#ef4444", backgroundColor: "white", border: "1px solid #ef4444" }}
            >
              uncabled port
            </span>
          )}
          {portsCabled !== false && pathValid === true && (
            <span
              className="text-xs px-1 py-0.5 rounded whitespace-nowrap"
              style={{ color: "#22c55e", backgroundColor: "white", border: "1px solid #22c55e" }}
            >
              {data?.pathHopCount} hops
            </span>
          )}
          {portsCabled !== false && pathValid === false && (
            <span
              className="text-xs px-1 py-0.5 rounded whitespace-nowrap"
              style={{ color: "#ef4444", backgroundColor: "white", border: "1px solid #ef4444" }}
            >
              no path
            </span>
          )}
        </div>
      </EdgeLabelRenderer>
    </>
  );
}
