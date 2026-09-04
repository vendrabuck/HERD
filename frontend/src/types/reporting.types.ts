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

// Purpose classification breakdowns (issue #646 phase 1). A null category
// arrives from the backend as the literal string "unclassified" (see
// lib/purposeCategories.ts), so `purpose_category` here is never null.
export interface PurposeBucket {
  purpose_category: string;
  reservations: number;
  device_hours: number;
}

export interface UserPurposeBucket {
  user_id: string;
  purpose_category: string;
  reservations: number;
  device_hours: number;
}

export interface DevicePurposeBucket {
  device_id: string;
  purpose_category: string;
  reservations: number;
  device_hours: number;
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
  // Optional: absent on a backend build that predates purpose classification.
  // The Purpose reporting section is gated on `by_purpose` alone.
  by_purpose?: PurposeBucket[];
  by_user_purpose?: UserPurposeBucket[];
  by_device_purpose?: DevicePurposeBucket[];
}

export interface UtilizationQuery {
  start: string;
  end: string;
  status?: ReservationStatus[];
}
