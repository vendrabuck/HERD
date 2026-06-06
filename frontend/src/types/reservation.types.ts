import type { TopologyType } from "./device.types";

export type ReservationStatus =
  | "PENDING"
  | "PENDING_PROVISION"
  | "ACTIVE"
  | "COMPLETED"
  | "CANCELLED"
  | "FAILED";

export interface Reservation {
  id: string;
  user_id: string;
  owner_name: string;
  device_ids: string[];
  topology_id: string | null;
  topology_type: TopologyType;
  purpose: string | null;
  start_time: string;
  end_time: string;
  status: ReservationStatus;
  created_at: string;
}

export interface ReservationCreate {
  device_ids: string[];
  topology_id?: string;
  purpose?: string;
  start_time: string;
  end_time: string;
}

export interface ReservationUpdate {
  end_time?: string;
  purpose?: string;
  device_ids?: string[];
}

export interface CalendarQueryParams {
  range_start: string;
  range_end: string;
  status?: ReservationStatus[];
  device_id?: string;
}
