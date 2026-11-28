import { useState } from "react";
import { useDeviceSearch } from "@/api/inventory";
import { DeviceSearchResults } from "@/components/devices/DeviceSearchResults";
import type { TopologyType } from "@/types/device.types";

export interface PickedDevice {
  id: string;
  name: string;
  topologyType: TopologyType;
}

export interface DeviceSearchPickerProps {
  /** Human label for the side, e.g. "Device A". Drives the accessible names. */
  label: string;
  device: PickedDevice | null;
  onSelect: (device: PickedDevice) => void;
  onClear: () => void;
}

/**
 * The compact device chooser above each port column. Same two-character
 * search-then-pick interaction as the single-pair modal on ConnectionsPage;
 * once a device is picked the column header underneath carries its name and
 * topology badge, so this collapses to just the label and a Change button.
 */
export function DeviceSearchPicker({ label, device, onSelect, onClear }: DeviceSearchPickerProps) {
  const [search, setSearch] = useState("");
  const { active, query: results } = useDeviceSearch(search);

  if (device) {
    return (
      <div className="flex items-center gap-2 min-w-0">
        <span className="text-xs font-medium text-gray-600 shrink-0">{label}</span>
        <button
          type="button"
          onClick={() => {
            setSearch("");
            onClear();
          }}
          className="text-xs text-blue-600 hover:text-blue-800 hover:underline"
        >
          Change
        </button>
      </div>
    );
  }

  return (
    <div className="relative">
      <input
        type="text"
        value={search}
        onChange={(e) => setSearch(e.target.value)}
        placeholder="Search devices..."
        aria-label={`Search ${label}`}
        className="w-full h-[30px] px-2 rounded-md border border-gray-300 text-xs focus:outline-none focus:ring-2 focus:ring-blue-500"
      />
      {active && (
        <DeviceSearchResults
          query={results}
          onPick={(d) => {
            setSearch("");
            onSelect({ id: d.id, name: d.name, topologyType: d.topology_type });
          }}
          className="max-h-40"
          itemClassName="px-2 py-1.5 text-xs"
        />
      )}
    </div>
  );
}
