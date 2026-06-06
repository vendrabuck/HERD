export type TopologyType = "PHYSICAL" | "CLOUD";
export type DeviceStatus = "AVAILABLE" | "RESERVED" | "OFFLINE" | "MAINTENANCE";

export interface Device {
  id: string;
  name: string;
  template_id: string;
  template_name: string | null;
  template_icon: string | null;
  template_vendor: string | null;
  template_model: string | null;
  template_part_number: string | null;
  topology_type: TopologyType;
  status: DeviceStatus;
  field_data: Record<string, unknown>;
  exclusive: boolean;
  driver_id: string | null;
  driver_name: string | null;
  connection_type: string | null;
  created_at: string;
  updated_at: string;
  created_by: string | null;
  created_by_name: string | null;
  modified_by: string | null;
  modified_by_name: string | null;
  poll_interval_seconds: number | null;
  resolved_poll_interval_seconds: number | null;
}

export interface DeviceCreate {
  name: string;
  template_id: string;
  topology_type: TopologyType;
  status?: DeviceStatus;
  field_data?: Record<string, unknown>;
  poll_interval_seconds?: number | null;
}

export interface DeviceUpdate {
  name?: string;
  topology_type?: TopologyType;
  status?: DeviceStatus;
  field_data?: Record<string, unknown>;
  poll_interval_seconds?: number | null;
}

export interface DeviceFilters {
  template_id?: string;
  topology_type?: TopologyType;
  status?: DeviceStatus;
  dut_only?: boolean;
  search?: string;
}
