export type DeviceType = "FIREWALL" | "SWITCH" | "ROUTER" | "TRAFFIC_SHAPER" | "OTHER";
export type TopologyType = "PHYSICAL" | "CLOUD";
export type DeviceStatus = "AVAILABLE" | "RESERVED" | "OFFLINE" | "MAINTENANCE";

export interface Device {
  id: string;
  name: string;
  device_type: DeviceType;
  topology_type: TopologyType;
  status: DeviceStatus;
  location: string | null;
  specs: Record<string, unknown> | null;
  description: string | null;
  created_at: string;
  updated_at: string;
}

export interface DeviceCreate {
  name: string;
  device_type: DeviceType;
  topology_type: TopologyType;
  status?: DeviceStatus;
  location?: string;
  specs?: Record<string, unknown>;
  description?: string;
}

export interface DeviceFilters {
  device_type?: DeviceType;
  topology_type?: TopologyType;
  status?: DeviceStatus;
}
