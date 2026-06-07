import { useState, useDeferredValue, useMemo } from "react";
import { useDevices } from "@/api/inventory";
import { useTemplates } from "@/api/templates";
import type { Device, TopologyType } from "@/types/device.types";

function TemplateIcon({ device }: { device: Device }) {
  if (device.template_icon) {
    return (
      <img
        src={device.template_icon}
        alt={device.template_name ?? ""}
        className="w-5 h-5 object-contain"
      />
    );
  }
  return <span className="inline-block w-5 h-5 bg-gray-300 rounded" />;
}

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
      <TemplateIcon device={device} />
      <div className="flex-1 min-w-0">
        <p className="text-sm font-medium truncate">{device.name}</p>
        <p className="text-xs text-gray-500 truncate">{device.template_name ?? "No template"}</p>
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

interface EquipmentBrowserProps {
  canvasDeviceIds?: string[];
}

export function EquipmentBrowser({ canvasDeviceIds = [] }: EquipmentBrowserProps) {
  const [templateFilter, setTemplateFilter] = useState("");
  const [topoFilter, setTopoFilter] = useState<TopologyType | "">("");
  const [search, setSearch] = useState("");
  const [showReserved, setShowReserved] = useState(true);
  const deferredSearch = useDeferredValue(search);

  const { data: templates } = useTemplates("device");
  const { data: devices, isLoading, isError } = useDevices({
    template_id: templateFilter || undefined,
    topology_type: topoFilter || undefined,
    dut_only: true,
    search: deferredSearch.trim() || undefined,
  });

  const canvasIdSet = useMemo(() => new Set(canvasDeviceIds), [canvasDeviceIds]);

  // Distinguish "inventory is genuinely empty" from "a filter matched nothing".
  // deferredSearch mirrors the value the device query actually ran with.
  const hasActiveFilter =
    Boolean(templateFilter) || Boolean(topoFilter) || deferredSearch.trim().length > 0;
  const inventoryEmpty = !devices || devices.length === 0;

  const filteredDevices = useMemo(() => {
    if (!devices) return [];

    return devices.filter((device) => {
      if (canvasIdSet.has(device.id)) return false;
      if (!showReserved && device.exclusive && device.status === "RESERVED") {
        return false;
      }
      return true;
    });
  }, [devices, canvasIdSet, showReserved]);

  return (
    <div className="flex flex-col w-56 max-h-[70vh] bg-gray-50 rounded-lg overflow-hidden">
      {/* Header */}
      <div className="px-3 py-3 border-b border-gray-200 bg-white">
        <h2 className="text-sm font-semibold text-gray-800 mb-2">Equipment Browser</h2>

        {/* Search */}
        <label htmlFor="eq-search" className="sr-only">Search devices</label>
        <input
          id="eq-search"
          type="text"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search devices..."
          className="w-full text-xs border border-gray-300 rounded px-2 py-1 mb-1.5 bg-white"
        />

        {/* Template filter */}
        <label htmlFor="eq-type-filter" className="sr-only">Template filter</label>
        <select
          id="eq-type-filter"
          value={templateFilter}
          onChange={(e) => setTemplateFilter(e.target.value)}
          className="w-full text-xs border border-gray-300 rounded px-2 py-1 mb-1.5 bg-white"
        >
          <option value="">All templates</option>
          {templates?.map((t) => (
            <option key={t.id} value={t.id}>
              {t.name}
            </option>
          ))}
        </select>

        {/* Topology filter */}
        <label htmlFor="eq-topo-filter" className="sr-only">Topology type filter</label>
        <select
          id="eq-topo-filter"
          value={topoFilter}
          onChange={(e) => setTopoFilter(e.target.value as TopologyType | "")}
          className="w-full text-xs border border-gray-300 rounded px-2 py-1 mb-1.5 bg-white"
        >
          <option value="">All lab types</option>
          <option value="PHYSICAL">Physical</option>
          <option value="CLOUD">Cloud</option>
        </select>

        {/* Show reserved toggle */}
        <label className="flex items-center gap-1.5 text-xs text-gray-600 mt-0.5">
          <input
            type="checkbox"
            checked={showReserved}
            onChange={(e) => setShowReserved(e.target.checked)}
            className="rounded border-gray-300"
          />
          Show reserved resources
        </label>
      </div>

      {/* Device list */}
      <div className="flex-1 overflow-y-auto p-2 space-y-1.5">
        {isLoading && (
          <p role="status" aria-live="polite" className="text-xs text-gray-500 text-center py-4">Loading devices...</p>
        )}
        {isError && (
          <p className="text-xs text-red-500 text-center py-4">Failed to load devices</p>
        )}
        {!isLoading && !isError && inventoryEmpty && !hasActiveFilter && (
          <p className="text-xs text-gray-400 text-center py-4">
            No devices in inventory. Ask an admin to add devices, or run the seed.
          </p>
        )}
        {!isLoading &&
          !isError &&
          filteredDevices.length === 0 &&
          !(inventoryEmpty && !hasActiveFilter) && (
            <p className="text-xs text-gray-400 text-center py-4">No devices found</p>
          )}
        {filteredDevices.map((device) => (
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
