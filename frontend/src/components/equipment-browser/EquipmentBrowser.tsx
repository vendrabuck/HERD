import { useState, useDeferredValue, useMemo } from "react";
import { Network, Waypoints, Cloud, Cable } from "lucide-react";
import { useDevices } from "@/api/inventory";
import { useTemplates } from "@/api/templates";
import type { Device, TopologyType } from "@/types/device.types";
import type { DeviceTemplate } from "@/types/template.types";
import type { NetworkElementType } from "@/types/topology.types";

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

function DynamicTemplateCard({ template }: { template: DeviceTemplate }) {
  const onDragStart = (e: React.DragEvent) => {
    e.dataTransfer.setData(
      "application/herd-dynamic-template",
      JSON.stringify({ id: template.id, name: template.name, icon: template.icon }),
    );
    e.dataTransfer.effectAllowed = "copy";
  };

  return (
    <div
      draggable
      onDragStart={onDragStart}
      className="flex items-center gap-2 p-2 rounded border border-dashed border-purple-300 bg-purple-50 cursor-grab active:cursor-grabbing hover:shadow-sm transition-shadow select-none"
      title="Drag onto canvas"
    >
      {template.icon ? (
        <img src={template.icon} alt={template.name} className="w-5 h-5 object-contain" />
      ) : (
        <span className="inline-block w-5 h-5 bg-gray-300 rounded" />
      )}
      <div className="flex-1 min-w-0">
        <p className="text-sm font-medium truncate">{template.name}</p>
        <p className="text-xs text-gray-500 truncate">Hypervisor-backed</p>
      </div>
      <span className="text-xs px-1 py-0.5 rounded shrink-0 bg-purple-200 text-purple-800">DYN</span>
    </div>
  );
}

// The closed v1 element vocabulary (ADR 0012 "Canvas shape"): four fixed
// palette entries with their own icons, mirroring ELEMENT_ICONS in
// NetworkElementNode.tsx/ElementAttachDialog.tsx.
const NETWORK_ELEMENT_TYPES: Array<{ type: NetworkElementType; label: string; icon: typeof Network }> = [
  { type: "vlan_segment", label: "VLAN segment", icon: Network },
  { type: "subnet", label: "Subnet", icon: Waypoints },
  { type: "external_cloud", label: "External cloud", icon: Cloud },
  { type: "patch_trunk", label: "Patch trunk", icon: Cable },
];

function NetworkElementCard({
  type,
  label,
  icon: Icon,
}: {
  type: NetworkElementType;
  label: string;
  icon: typeof Network;
}) {
  const onDragStart = (e: React.DragEvent) => {
    e.dataTransfer.setData(
      "application/herd-network-element",
      JSON.stringify({ element_type: type, label }),
    );
    e.dataTransfer.effectAllowed = "copy";
  };

  return (
    <div
      draggable
      onDragStart={onDragStart}
      className="flex items-center gap-2 p-2 rounded border border-dashed border-gray-300 bg-gray-50 cursor-grab active:cursor-grabbing hover:shadow-sm transition-shadow select-none"
      title="Drag onto canvas"
    >
      <span className="inline-flex items-center justify-center w-5 h-5 rounded bg-gray-200 text-gray-600 shrink-0">
        <Icon className="w-3.5 h-3.5" />
      </span>
      <div className="flex-1 min-w-0">
        <p className="text-sm font-medium truncate">{label}</p>
        <p className="text-xs text-gray-500 truncate">Reachability hub</p>
      </div>
    </div>
  );
}

function ChevronIcon({ expanded }: { expanded: boolean }) {
  return (
    <svg
      className={`w-4 h-4 text-gray-400 transition-transform ${expanded ? "rotate-90" : ""}`}
      fill="none"
      viewBox="0 0 24 24"
      stroke="currentColor"
      strokeWidth={2}
    >
      <path strokeLinecap="round" strokeLinejoin="round" d="M9 5l7 7-7 7" />
    </svg>
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
  const [showDynamic, setShowDynamic] = useState(true);
  const [showNetworkElements, setShowNetworkElements] = useState(true);
  const deferredSearch = useDeferredValue(search);

  const { data: templates } = useTemplates("device");
  // Separate query on purpose: the device list's dut_only shape stays untouched.
  const { data: dynamicTemplates } = useTemplates("dynamic");
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

      {/* Dynamic templates: hypervisor-backed, no physical device. Absent when
          no dynamic templates exist. */}
      {dynamicTemplates && dynamicTemplates.length > 0 && (
        <div className="border-t border-gray-200 bg-white">
          <button
            type="button"
            onClick={() => setShowDynamic((v) => !v)}
            aria-expanded={showDynamic}
            className="w-full flex items-center justify-between px-3 py-2 text-xs font-semibold text-gray-700 hover:bg-gray-50 transition-colors"
          >
            Dynamic templates ({dynamicTemplates.length})
            <ChevronIcon expanded={showDynamic} />
          </button>
          {showDynamic && (
            <div className="px-2 pb-2 space-y-1.5 max-h-40 overflow-y-auto">
              {dynamicTemplates.map((t) => (
                <DynamicTemplateCard key={t.id} template={t} />
              ))}
            </div>
          )}
        </div>
      )}

      {/* Network elements: the four v1 types are static, not fetched, so
          unlike the dynamic-templates section this one always renders (ADR
          0012 "Editing surface"). */}
      <div className="border-t border-gray-200 bg-white">
        <button
          type="button"
          onClick={() => setShowNetworkElements((v) => !v)}
          aria-expanded={showNetworkElements}
          className="w-full flex items-center justify-between px-3 py-2 text-xs font-semibold text-gray-700 hover:bg-gray-50 transition-colors"
        >
          Network elements
          <ChevronIcon expanded={showNetworkElements} />
        </button>
        {showNetworkElements && (
          <div className="px-2 pb-2 space-y-1.5">
            {NETWORK_ELEMENT_TYPES.map((entry) => (
              <NetworkElementCard key={entry.type} type={entry.type} label={entry.label} icon={entry.icon} />
            ))}
          </div>
        )}
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
