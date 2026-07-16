import type { TopologyType } from "./device.types";

export type ReservationStatus =
  | "PENDING"
  | "PENDING_PROVISION"
  | "ACTIVE"
  | "COMPLETED"
  | "CANCELLED"
  | "FAILED";

// One requested dynamic (hypervisor-backed) instance; mirrors the backend
// DynamicRequestSpec (ADR 0004, issue #274). Listing the same template_id N
// times requests N instances of it; the backend deliberately does not dedupe.
export interface DynamicRequestSpec {
  template_id: string;
}

// A booked dynamic instance request; id is the execution-side request_id.
// Mirrors the backend DynamicRequestResponse.
export interface DynamicRequestResponse {
  id: string;
  template_id: string;
}

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
  // Non-null only when an admin cancelled a reservation they do not own (#340).
  // Optional here so fixtures and pre-#340 cached responses stay valid.
  cancelled_by?: string | null;
  // Always present in backend responses (defaults to []); optional here so
  // fixtures and cached responses that predate dynamic requests stay valid.
  dynamic_requests?: DynamicRequestResponse[];
}

export interface ReservationCreate {
  // May be empty for a dynamic-only booking; the backend requires at least
  // one device or one dynamic request across the two fields.
  device_ids: string[];
  topology_id?: string;
  purpose?: string;
  start_time: string;
  end_time: string;
  // Send only when non-empty; the backend defaults an absent field to [].
  // Capped at 50 entries server-side (tighter than the 200-device cap).
  dynamic_requests?: DynamicRequestSpec[];
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
