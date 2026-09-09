import { Handle, Position, type NodeProps } from "@xyflow/react";
import type { DeviceNode as DeviceNodeType } from "@/types/topology.types";
import { TopoBadge } from "@/components/ui/TopoBadge";
import { l3RoutesOf } from "@/lib/canvasNodes";
import { cn } from "@/lib/cn";

const TOPOLOGY_COLORS: Record<string, string> = {
  PHYSICAL: "bg-blue-100 border-blue-400 text-blue-900",
  CLOUD: "bg-purple-100 border-purple-400 text-purple-900",
};

export function DeviceNode({ data, selected }: NodeProps<DeviceNodeType>) {
  const { device, isProposal, l3ValidationInvalid } = data;
  const colorClass = TOPOLOGY_COLORS[device.topology_type] ?? "bg-gray-100 border-gray-400";
  // Review fix F6 (issue #34): l3RoutesOf never throws on a malformed
  // persisted/imported `data.l3` ({} or {routes: null}); it reads [] instead
  // of crashing this node into the ErrorBoundary with no way back into the
  // editor to fix it.
  const routeCount = l3RoutesOf(data).length;

  return (
    <div
      className={`
        relative rounded-lg border-2 p-3 min-w-[140px] shadow-sm cursor-grab
        ${colorClass}
        ${selected ? "ring-2 ring-offset-1 ring-yellow-400" : ""}
        ${isProposal ? "border-dashed opacity-70" : ""}
      `}
    >
      {isProposal && (
        <span className="absolute -top-2 -right-2 text-[10px] font-bold px-1.5 py-0.5 rounded bg-purple-600 text-white shadow">
          PROPOSED
        </span>
      )}
      {routeCount > 0 && (
        // ADR 0014 Decision 4 (issue #34): a route-count badge so routing
        // intent is visible on the canvas without opening the Routing panel.
        // Red variant when the last validation run reported a BLOCKING
        // problem for this node (review fix F1: an l3_duplicate_route-only
        // result is informational and does not turn this red). Not built on
        // components/ui/StatusBadge.tsx (review fix F11 considered it): that
        // component is keyed by a fixed backend enum string with a pale
        // -100/-800 tone, for an inline status pill; this is a solid overlay
        // chip anchored to the canvas node's corner (the same treatment as
        // the PROPOSED badge above), showing a dynamic count rather than one
        // of a closed set of enum values, so it stays a `cn()`-composed span
        // instead.
        <span
          className={cn(
            "absolute -top-2 -left-2 text-[10px] font-bold px-1.5 py-0.5 rounded shadow",
            l3ValidationInvalid ? "bg-red-600 text-white" : "bg-slate-600 text-white",
          )}
        >
          {routeCount} route{routeCount === 1 ? "" : "s"}
        </span>
      )}
      <Handle type="source" id="top" position={Position.Top} className="!bg-gray-500" />
      <Handle type="source" id="right" position={Position.Right} className="!bg-gray-500" />

      <div className="flex flex-col items-center gap-1">
        {device.template_icon ? (
          <img
            src={device.template_icon}
            alt={device.template_name ?? ""}
            className="w-8 h-8 object-contain"
          />
        ) : (
          <span className="inline-block w-8 h-8 bg-gray-300 rounded" />
        )}
        <span className="text-sm font-semibold text-center leading-tight">{device.name}</span>
        <TopoBadge type={device.topology_type} variant="onCanvas" />
        <span className="text-xs text-gray-500">{device.template_name ?? ""}</span>
        {device.status !== "AVAILABLE" && (
          <span className="text-xs text-red-600 font-medium">{device.status}</span>
        )}
      </div>

      <Handle type="source" id="bottom" position={Position.Bottom} className="!bg-gray-500" />
      <Handle type="source" id="left" position={Position.Left} className="!bg-gray-500" />
    </div>
  );
}
