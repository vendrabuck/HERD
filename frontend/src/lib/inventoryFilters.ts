import type { DeviceStatus, TopologyType } from "@/types/device.types";

// Values the inventory list endpoint accepts for `status` and `topology_type`
// (herd_common.enums.TopologyType and inventory's DeviceStatus). A saved value
// outside these sets is dropped on read, so a stale preference can never reach
// the API and 422.
export const DEVICE_STATUSES: readonly DeviceStatus[] = [
  "AVAILABLE",
  "RESERVED",
  "OFFLINE",
  "MAINTENANCE",
];
export const TOPOLOGY_TYPES: readonly TopologyType[] = ["PHYSICAL", "CLOUD"];

export interface InventoryFilterState {
  search: string;
  status: DeviceStatus | "";
  templateId: string;
  topologyType: TopologyType | "";
}

// The persisted `savedFilters.inventory` object. `search` is always written (the
// original shape was `{ search }`); a filter set to "All" is omitted, so an old
// `{ search }` object and a new one with every filter at "All" are identical.
export interface InventorySavedFilter {
  search?: string;
  status?: string;
  template_id?: string;
  topology_type?: string;
}

export function parseSavedInventoryFilter(raw: unknown): InventoryFilterState {
  const obj = raw !== null && typeof raw === "object" ? (raw as Record<string, unknown>) : {};
  const status = DEVICE_STATUSES.find((s) => s === obj.status) ?? "";
  const topologyType = TOPOLOGY_TYPES.find((t) => t === obj.topology_type) ?? "";
  return {
    search: typeof obj.search === "string" ? obj.search : "",
    status,
    templateId: typeof obj.template_id === "string" ? obj.template_id : "",
    topologyType,
  };
}

export function serializeInventoryFilter(state: InventoryFilterState): InventorySavedFilter {
  const out: InventorySavedFilter = { search: state.search };
  if (state.status) out.status = state.status;
  if (state.templateId) out.template_id = state.templateId;
  if (state.topologyType) out.topology_type = state.topologyType;
  return out;
}
