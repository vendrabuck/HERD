import type { TopologyType } from "./device.types";

export type ReservationStatus = "PENDING" | "ACTIVE" | "COMPLETED" | "CANCELLED";

export interface Reservation {
  id: string;
  user_id: string;
  device_ids: string[];
  topology_type: TopologyType;
  purpose: string | null;
  start_time: string;
  end_time: string;
  status: ReservationStatus;
  created_at: string;
}

export interface ReservationCreate {
  device_ids: string[];
  purpose?: string;
  start_time: string;
  end_time: string;
}
