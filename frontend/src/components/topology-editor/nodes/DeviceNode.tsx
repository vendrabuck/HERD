import { Handle, Position, type NodeProps } from "@xyflow/react";
import type { DeviceNode as DeviceNodeType } from "@/types/topology.types";
import { TopoBadge } from "@/components/ui/TopoBadge";

const TOPOLOGY_COLORS: Record<string, string> = {
  PHYSICAL: "bg-blue-100 border-blue-400 text-blue-900",
  CLOUD: "bg-purple-100 border-purple-400 text-purple-900",
};

export function DeviceNode({ data, selected }: NodeProps<DeviceNodeType>) {
  const { device, isProposal } = data;
  const colorClass = TOPOLOGY_COLORS[device.topology_type] ?? "bg-gray-100 border-gray-400";

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
