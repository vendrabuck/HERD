import type { ReservationStatus } from "./reservation.types";

export interface UserBucket {
  user_id: string;
  owner_name: string;
  reservation_count: number;
  hours: number;
}

export interface DeviceBucket {
  device_id: string;
  reservation_count: number;
  hours: number;
}

export interface TopologyTypeBucket {
  topology_type: string;
  reservation_count: number;
  hours: number;
}

export interface DayBucket {
  day: string;
  reservation_count: number;
  hours: number;
}

export interface GroupBucket {
  group_id: string | null;
  group_name: string;
  reservation_count: number;
  hours: number;
}

export interface FleetDeviceBucket {
  device_id: string;
  name: string;
  status: string;
  reservation_count: number;
  hours: number;
  utilization_pct: number;
}

export interface FleetSection {
  device_count: number;
  idle_device_count: number;
  window_hours: number;
  total_reserved_hours: number;
  utilization_pct: number;
  devices: FleetDeviceBucket[];
}

export interface UtilizationReport {
  window_start: string;
  window_end: string;
  total_hours: number;
  total_reservations: number;
  by_user: UserBucket[];
  by_device: DeviceBucket[];
  by_topology_type: TopologyTypeBucket[];
  by_day: DayBucket[];
  by_group: GroupBucket[];
  execution_run_count: number | null;
  // null when the inventory service was unreachable server-side.
  fleet: FleetSection | null;
}

export interface UtilizationQuery {
  start: string;
  end: string;
  status?: ReservationStatus[];
}
