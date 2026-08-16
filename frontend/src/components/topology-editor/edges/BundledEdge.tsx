import { useMemo, useState } from "react";
import { X } from "lucide-react";
import { BaseEdge, EdgeLabelRenderer, getBezierPath, type EdgeProps, type Edge } from "@xyflow/react";
import { useTopologyStore } from "@/stores/topologyStore";
import type { BundledEdgeData } from "./groupEdgesForRender";
import { resolveEdgeStroke, INVALID_STROKE } from "./edgeStatus";

const NEUTRAL_STROKE = "#4b5563";

/**
 * Render-only rendering of two or more edges sharing a device pair (the
 * store still holds every underlying edge as its own object; see
 * groupEdgesForRender). One thick line plus a count badge that expands into
 * the full list.
 *
 * Status derivation (issue #517 review item 4b): the bundle must never mask
 * an invalid member behind a flat neutral color. If ANY member is invalid
 * (an uncabled port or an unreachable path, the same precedence LayerEdge
 * uses via resolveEdgeStroke), the whole bundle renders red; the expanded
 * list also shows each member's own status label so the signal survives
 * being folded into one visual line.
 */
export function BundledEdge({
  sourceX,
  sourceY,
  targetX,
  targetY,
  sourcePosition,
  targetPosition,
  data,
}: EdgeProps<Edge<BundledEdgeData>>) {
  const [expanded, setExpanded] = useState(false);
  const removeEdge = useTopologyStore((s) => s.removeEdge);
  const members = useMemo(() => data?.members ?? [], [data?.members]);
  // Memoized (issue #517 review item 10c): recomputing every member's
  // stroke on every render (e.g. purely from the expand/collapse toggle,
  // which does not change `data` at all) is wasted work.
  const memberStrokes = useMemo(() => members.map((m) => resolveEdgeStroke(m.data)), [members]);
  const anyInvalid = useMemo(() => memberStrokes.some((s) => s.isInvalid), [memberStrokes]);
  const bundleStroke = anyInvalid ? INVALID_STROKE : NEUTRAL_STROKE;

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
      <BaseEdge path={edgePath} style={{ stroke: bundleStroke, strokeWidth: 3 }} />
      <EdgeLabelRenderer>
        <div
          style={{
            position: "absolute",
            transform: `translate(-50%, -50%) translate(${labelX}px,${labelY}px)`,
            pointerEvents: "all",
          }}
          className="nodrag nopan"
        >
          <button
            type="button"
            onClick={() => setExpanded((v) => !v)}
            className="text-xs font-medium px-1.5 py-0.5 rounded bg-white whitespace-nowrap"
            style={{ border: `1px solid ${bundleStroke}`, color: anyInvalid ? INVALID_STROKE : "#111827" }}
          >
            <span className="font-bold">{members.length}</span> connections
          </button>
          {expanded && (
            <div className="mt-1 w-56 max-h-48 overflow-y-auto bg-white border border-gray-200 rounded-md shadow-xl">
              {members.map((member, idx) => {
                const status = memberStrokes[idx];
                const layer = member.data?.layer ?? "L2";
                const sourceName = member.data?.source_port_name ?? "?";
                const targetName = member.data?.target_port_name ?? "?";
                return (
                  <div
                    key={member.id}
                    className="flex flex-col gap-0.5 px-2 py-1 border-b border-gray-100 last:border-b-0 text-xs"
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="font-mono text-gray-700 truncate">
                        {sourceName} to {targetName}
                      </span>
                      <span
                        className="text-[11px] font-bold px-1 py-0.5 rounded border bg-white shrink-0"
                        style={{ color: status.stroke, borderColor: status.stroke }}
                      >
                        {layer}
                      </span>
                      {/* Per-member delete (review item 2): removes just this
                          one connection from the store; the bundle-level
                          Delete (React Flow's own key handling, expanded via
                          handleEdgesChange) keeps removing every member.
                          Hidden in read-only mode (review round 3 item 3):
                          this button calls the store directly, so it does
                          NOT inherit the ReactFlow component's own
                          elementsSelectable/deleteKeyCode read-only gating
                          the way bundle-level Delete does; an archived
                          fork's as-built canvas must stay non-editable. */}
                      {!data?.isReadOnly && (
                        <button
                          type="button"
                          onClick={() => removeEdge(member.id)}
                          aria-label={`Remove ${sourceName} to ${targetName}`}
                          className="p-0.5 rounded text-gray-400 hover:text-red-600 hover:bg-red-50 shrink-0"
                        >
                          <X className="w-3 h-3" />
                        </button>
                      )}
                    </div>
                    {status.statusLabel && (
                      <span className="text-[10px]" style={{ color: status.stroke }}>
                        {status.statusLabel}
                      </span>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </EdgeLabelRenderer>
    </>
  );
}
