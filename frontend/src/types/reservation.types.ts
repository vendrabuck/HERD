import type { TopologyType } from "./device.types";
import type { CanvasData } from "./topology.types";

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

// Fork status mirrors the cabling ReservationFork.status column (ADR 0006).
// ACTIVE forks are editable; ARCHIVED forks are the frozen as-built record and
// refuse every mutation server-side.
export type ForkStatus = "ACTIVE" | "ARCHIVED";

// One row of a fork's reconciled wiring, from GET /reservations/{id}/fork.
export interface ForkConnection {
  id: string;
  device_a_id: string;
  port_a: string;
  device_b_id: string;
  port_b: string;
  layer: string;
  physical_connection_id: string | null;
  created_by: string;
  created_at: string;
}

// A fork_versions row without its canvas payload; the History list in live-edit
// mode renders these instead of the parent topology's TopologyVersion rows.
export interface ForkVersionSummary {
  id: string;
  fork_id: string;
  version_number: number;
  restored_from_id: string | null;
  created_at: string;
}

// GET /reservations/{id}/fork: the editable/as-built fork for a reservation.
export interface ReservationFork {
  id: string;
  reservation_id: string;
  parent_topology_id: string | null;
  parent_version_id: string | null;
  status: ForkStatus;
  canvas_data: CanvasData | null;
  created_at: string;
  updated_at: string;
  connections: ForkConnection[];
  versions: ForkVersionSummary[];
}

// PUT /reservations/{id}/fork/canvas: the loose-draft store result (no version,
// no reconcile). Carries only route-shape validation so the editor can flag
// unreachable edges; the draft stores regardless.
export interface ForkCanvasDraftResult {
  id: string;
  valid: boolean;
  invalid_edges: unknown[];
}

// One released or built wire in a save result (canonical connection identity).
export interface ForkConnectionDelta {
  device_a_id: string;
  port_a: string;
  device_b_id: string;
  port_b: string;
  layer: string;
}

// POST /reservations/{id}/fork/save: the reconcile result. The parent topology's
// history is untouched; the released/built delta and the new fork version are the
// only durable effects.
export interface ForkSaveResult {
  fork_id: string;
  version_number: number;
  released: ForkConnectionDelta[];
  built: ForkConnectionDelta[];
  unchanged_count: number;
}

// One port already claimed by another ACTIVE reservation, from a save 409.
export interface ForkPortConflict {
  reservation_id: string;
  device_id: string;
  port: string;
}

// The structured detail of a cross-reservation port-claim 409 (ADR 0006
// Decision 4), relayed verbatim through reservations from cabling.
export interface ForkConflictDetail {
  message: string;
  conflicts: ForkPortConflict[];
}
