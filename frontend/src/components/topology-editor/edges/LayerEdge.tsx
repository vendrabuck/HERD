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
  const style = LAYER_STYLES[layer] ?? LAYER_STYLES.L2;

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
          strokeWidth: 2,
          strokeDasharray: style.strokeDasharray,
        }}
      />
      <EdgeLabelRenderer>
        <div
          style={{
            position: "absolute",
            transform: `translate(-50%, -50%) translate(${labelX}px,${labelY}px)`,
            pointerEvents: "all",
          }}
          className="nodrag nopan"
        >
          <span
            className="text-xs font-bold px-1 py-0.5 rounded"
            style={{ color: style.stroke, backgroundColor: "white", border: `1px solid ${style.stroke}` }}
          >
            {style.label}
          </span>
        </div>
      </EdgeLabelRenderer>
    </>
  );
}
