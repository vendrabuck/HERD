import { Handle, Position, type NodeProps } from "@xyflow/react";
import type { DeviceNode as DeviceNodeType } from "@/types/topology.types";

const DEVICE_ICONS: Record<string, string> = {
  FIREWALL: "🔥",
  SWITCH: "⧉",
  ROUTER: "↔",
  TRAFFIC_SHAPER: "≋",
  OTHER: "⬡",
};

const TOPOLOGY_COLORS: Record<string, string> = {
  PHYSICAL: "bg-blue-100 border-blue-400 text-blue-900",
  CLOUD: "bg-purple-100 border-purple-400 text-purple-900",
};

const TOPOLOGY_BADGE: Record<string, string> = {
  PHYSICAL: "bg-blue-200 text-blue-800",
  CLOUD: "bg-purple-200 text-purple-800",
};

export function DeviceNode({ data, selected }: NodeProps<DeviceNodeType>) {
  const { device } = data;
  const icon = DEVICE_ICONS[device.device_type] ?? "⬡";
  const colorClass = TOPOLOGY_COLORS[device.topology_type] ?? "bg-gray-100 border-gray-400";
  const badgeClass = TOPOLOGY_BADGE[device.topology_type] ?? "bg-gray-200 text-gray-800";

  return (
    <div
      className={`
        relative rounded-lg border-2 p-3 min-w-[140px] shadow-sm cursor-grab
        ${colorClass}
        ${selected ? "ring-2 ring-offset-1 ring-yellow-400" : ""}
      `}
    >
      <Handle type="target" position={Position.Top} className="!bg-gray-500" />

      <div className="flex flex-col items-center gap-1">
        <span className="text-2xl">{icon}</span>
        <span className="text-sm font-semibold text-center leading-tight">{device.name}</span>
        <span
          className={`text-xs px-1.5 py-0.5 rounded font-medium ${badgeClass}`}
        >
          {device.topology_type}
        </span>
        <span className="text-xs text-gray-500">{device.device_type.replace("_", " ")}</span>
        {device.status !== "AVAILABLE" && (
          <span className="text-xs text-red-600 font-medium">{device.status}</span>
        )}
      </div>

      <Handle type="source" position={Position.Bottom} className="!bg-gray-500" />
    </div>
  );
}
