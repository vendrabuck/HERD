import { useState } from "react";
import { useDevices } from "@/api/inventory";
import type { Device, DeviceType, TopologyType } from "@/types/device.types";

const DEVICE_ICONS: Record<DeviceType, string> = {
  FIREWALL: "🔥",
  SWITCH: "⧉",
  ROUTER: "↔",
  TRAFFIC_SHAPER: "≋",
  OTHER: "⬡",
};

const DEVICE_TYPES: { label: string; value: DeviceType }[] = [
  { label: "Firewall", value: "FIREWALL" },
  { label: "Switch", value: "SWITCH" },
  { label: "Router", value: "ROUTER" },
  { label: "Traffic Shaper", value: "TRAFFIC_SHAPER" },
  { label: "Other", value: "OTHER" },
];

function DeviceCard({ device }: { device: Device }) {
  const isAvailable = device.status === "AVAILABLE";

  const onDragStart = (e: React.DragEvent) => {
    if (!isAvailable) {
      e.preventDefault();
      return;
    }
    e.dataTransfer.setData("application/herd-device", JSON.stringify(device));
    e.dataTransfer.effectAllowed = "copy";
  };

  const topologyColor =
    device.topology_type === "PHYSICAL"
      ? "border-blue-300 bg-blue-50"
      : "border-purple-300 bg-purple-50";

  return (
    <div
      draggable={isAvailable}
      onDragStart={onDragStart}
      className={`
        flex items-center gap-2 p-2 rounded border cursor-grab active:cursor-grabbing
        hover:shadow-sm transition-shadow select-none
        ${topologyColor}
        ${device.status !== "AVAILABLE" ? "opacity-50 cursor-not-allowed" : ""}
      `}
      title={device.status !== "AVAILABLE" ? `Not available: ${device.status}` : "Drag onto canvas"}
    >
      <span className="text-lg">{DEVICE_ICONS[device.device_type]}</span>
      <div className="flex-1 min-w-0">
        <p className="text-sm font-medium truncate">{device.name}</p>
        <p className="text-xs text-gray-500 truncate">{device.location ?? "No location"}</p>
      </div>
      <span
        className={`text-xs px-1 py-0.5 rounded shrink-0 ${
          device.topology_type === "PHYSICAL"
            ? "bg-blue-200 text-blue-800"
            : "bg-purple-200 text-purple-800"
        }`}
      >
        {device.topology_type === "PHYSICAL" ? "PHY" : "CLD"}
      </span>
    </div>
  );
}

export function EquipmentBrowser() {
  const [typeFilter, setTypeFilter] = useState<DeviceType | "">("");
  const [topoFilter, setTopoFilter] = useState<TopologyType | "">("");

  const { data: devices, isLoading, isError } = useDevices({
    device_type: typeFilter || undefined,
    topology_type: topoFilter || undefined,
    status: "AVAILABLE",
  });

  return (
    <div className="flex flex-col h-full bg-gray-50 border-r border-gray-200">
      {/* Header */}
      <div className="px-3 py-3 border-b border-gray-200 bg-white">
        <h2 className="text-sm font-semibold text-gray-800 mb-2">Equipment Browser</h2>

        {/* Type filter */}
        <select
          value={typeFilter}
          onChange={(e) => setTypeFilter(e.target.value as DeviceType | "")}
          className="w-full text-xs border border-gray-300 rounded px-2 py-1 mb-1.5 bg-white"
        >
          <option value="">All types</option>
          {DEVICE_TYPES.map((t) => (
            <option key={t.value} value={t.value}>
              {t.label}
            </option>
          ))}
        </select>

        {/* Topology filter */}
        <select
          value={topoFilter}
          onChange={(e) => setTopoFilter(e.target.value as TopologyType | "")}
          className="w-full text-xs border border-gray-300 rounded px-2 py-1 bg-white"
        >
          <option value="">All lab types</option>
          <option value="PHYSICAL">Physical</option>
          <option value="CLOUD">Cloud</option>
        </select>
      </div>

      {/* Device list */}
      <div className="flex-1 overflow-y-auto p-2 space-y-1.5">
        {isLoading && (
          <p className="text-xs text-gray-500 text-center py-4">Loading devices...</p>
        )}
        {isError && (
          <p className="text-xs text-red-500 text-center py-4">Failed to load devices</p>
        )}
        {devices?.length === 0 && (
          <p className="text-xs text-gray-400 text-center py-4">No devices found</p>
        )}
        {devices?.map((device) => (
          <DeviceCard key={device.id} device={device} />
        ))}
      </div>

      {/* Legend */}
      <div className="px-3 py-2 border-t border-gray-200 bg-white">
        <p className="text-xs text-gray-400 mb-1">Drag devices onto canvas</p>
        <div className="flex gap-2">
          <span className="flex items-center gap-1 text-xs">
            <span className="w-2 h-2 rounded-full bg-blue-400 inline-block" />
            Physical
          </span>
          <span className="flex items-center gap-1 text-xs">
            <span className="w-2 h-2 rounded-full bg-purple-400 inline-block" />
            Cloud
          </span>
        </div>
      </div>
    </div>
  );
}
