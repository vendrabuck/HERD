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

// AI purpose-classification distribution and metadata (issue #646 phase 2,
// ADR 0013 point 9). Shared by the interactive creation-pass preview
// (POST /ai/classify-purpose/preview) and the background end-of-reservation
// pass stored as Reservation.purpose_suggestion; `pass` is the only field
// that distinguishes which one produced a given value.
export interface PurposeDistributionEntry {
  category: string;
  probability: number;
}

export interface PurposeClassification {
  distribution: PurposeDistributionEntry[];
  top_category: string;
  pass: "creation" | "end";
  model: string;
  rationale: string;
  generated_at: string;
  signals_used: string[];
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
  // Lab purpose classification (issue #646 phase 1). A plain string column
  // validated at write time against the server's configurable category list
  // (GET /reservations/purpose-categories); null means unclassified. Optional
  // here so fixtures and cached responses that predate the feature stay valid.
  purpose_category?: string | null;
  purpose_category_set_at?: string | null;
  // AI purpose suggestion (issue #646 phase 2, ADR 0013 points 8-9). Null
  // when no pass has produced one yet. `purpose_suggestion_dismissed_at` is
  // set only by the admin review page's Dismiss action; a dismissed
  // suggestion stays on the reservation (ai_suggested state persists) but
  // is excluded from the review queue. Optional so fixtures and cached
  // responses that predate the feature stay valid.
  purpose_suggestion?: PurposeClassification | null;
  purpose_suggested_at?: string | null;
  purpose_suggestion_dismissed_at?: string | null;
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
  // Optional purpose classification (issue #646 phase 1); null or omitted
  // means unclassified.
  purpose_category?: string | null;
}

// GET /reservations/purpose-categories: the server-configured taxonomy
// (default list, or its PURPOSE_CATEGORIES env override). Never hardcode this
// list for validation or rendering; it can change without a frontend deploy.
export interface PurposeCategoriesResponse {
  categories: string[];
}

export interface ReservationUpdate {
  end_time?: string;
  purpose?: string;
  device_ids?: string[];
}

// POST /ai/classify-purpose/preview request body (issue #646 phase 2). Sent
// from the create-reservation modal; every optional signal is null rather
// than omitted when unavailable, matching the fixed contract.
export interface PurposePreviewRequest {
  categories: string[];
  purpose: string | null;
  topology_id: string | null;
  device_ids: string[] | null;
  dynamic_requests: { template_id: string; count: number }[] | null;
}

// GET /reservations/admin/purpose-review item and list envelope (issue #646
// phase 2, ADR 0013 point 10). Admin-only; rows are reservations that carry a
// suggestion still awaiting accept/dismiss.
export interface PurposeReviewItem {
  reservation_id: string;
  user_id: string;
  purpose: string | null;
  start_time: string;
  end_time: string;
  status: ReservationStatus;
  purpose_category: string | null;
  purpose_suggestion: PurposeClassification | null;
  purpose_suggested_at: string | null;
  device_count: number;
}

export interface PurposeReviewResponse {
  items: PurposeReviewItem[];
  total: number;
  skip: number;
  limit: number;
}

// POST /reservations/admin/purpose/backfill response.
export interface PurposeBackfillResponse {
  marked: number;
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

// GET /reservations/{id}/fork/versions/{version_id}: one historical version's
// full canvas snapshot (issue #622). Same fields as ForkVersionSummary plus the
// canvas payload, used for read-only preview and the client-side diff.
export interface ForkVersionDetail extends ForkVersionSummary {
  canvas_data: CanvasData | null;
}

// GET /reservations/{id}/fork: the editable/as-built fork for a reservation.
// draft_restored_from_id (issue #622 contract revision, 2026-08-28): non-null
// only while the draft canvas holds a restored-but-unsaved snapshot (set by
// POST .../restore, cleared the moment the next Save appends the version that
// carries restored_from_id instead). Restore never appends a version itself;
// this field is how the UI shows "unsaved restore" in the meantime.
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
  draft_restored_from_id: string | null;
}

// PUT /reservations/{id}/fork/canvas: the loose-draft store result (no version,
// no reconcile). Carries only route-shape validation so the editor can flag
// unreachable edges; the draft stores regardless.
export interface ForkCanvasDraftResult {
  id: string;
  valid: boolean;
  invalid_edges: unknown[];
}

// POST /reservations/{id}/fork/versions/{version_id}/restore: restore-to-draft
// (ADR 0006 addendum, issue #622; contract revised 2026-08-28: restore is a
// SAVE-less draft replace, so it appends no fork_versions row). ForkCanvasUpdateResponse-shaped
// (id, valid, invalid_edges: the restored draft's route validation) plus
// draft_restored_from_id, the id of the version that was restored (mirrors
// ReservationFork.draft_restored_from_id, which the fork refetch this
// triggers will now also carry). The "restored" marker
// (ForkVersionSummary.restored_from_id) appears only once the user runs Save,
// on the NEW version that save creates. Restore never wires anything; the
// caller re-fetches the version's own canvas_data
// (GET .../versions/{version_id}) to load the restored draft, since this
// response does not carry the canvas payload itself.
export interface ForkVersionRestoreResult extends ForkCanvasDraftResult {
  draft_restored_from_id: string;
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
  // ADR 0012 phase 1: count of device-to-element edges the resolver skipped
  // explicitly (declarative attachments never become a wiring hop). Optional
  // since the backend defaults it to 0 and older responses may omit it.
  element_attachments_skipped?: number;
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

// The structured detail of a fork endpoint-membership 409 (issue #701): a save
// or create whose canvas names a device outside the reservation's device set,
// relayed verbatim through reservations from cabling.
export interface ForkDeviceNotMemberDetail {
  error: "fork_device_not_member";
  device_ids: string[];
}

// --- Layered per-connection wiring status (ADR 0007 / ADR 0009) -------------
// After a fork save reconciles the intended wiring, execution applies each layer
// connection-by-connection and records the applied state in its wiring ledgers
// (l1_connection_assignments, l2_port_assignments, route_assignments). These
// types mirror the execution-side WiringStatusResponse / WiringRetryResponse
// that reservations proxies at GET /reservations/{id}/wiring-status and
// POST /reservations/{id}/wiring/retry.

export type WiringConnectionState = "ACTIVE" | "RELEASED" | "FAILED";

export type WiringLayer = "l1" | "l2" | "l3";

// One wiring-ledger row and its applied state. Layered since ADR 0009 phase 8:
// `layer` is "l1" for a switch cross-connect (port_a/port_b populated), "l2"
// for a VLAN membership (port/vlan populated), or "l3" for a per-switch route
// pin (route_count populated). `layer` is optional only for tolerance of a
// pre-phase-8 backend during a rolling deploy; treat an absent layer as "l1".
// `intended` (ADR 0009, issue #369) is the direction the row's last write was
// attempting (ACTIVE build vs RELEASED teardown), so a FAILED release after the
// reservation ends is distinguishable from a FAILED build.
// `retryable` is true only for a FAILED row whose failure is a transient driver
// error (hardware-retryable); a FAILED row with retryable=false is an
// unresolvable/not-a-simple-chain intent whose recovery is a fork re-save
// (ADR 0007 Decision 5/6), not a hardware retry.
export interface WiringConnectionStatus {
  id: string;
  switch_device_id: string;
  layer?: WiringLayer;
  // L1 cross-connect fields (present for layer "l1").
  port_a?: string | null;
  port_b?: string | null;
  physical_connection_id?: string | null;
  // L2 membership fields (present for layer "l2").
  port?: string | null;
  vlan_assignment_id?: string | null;
  vlan?: number | null;
  // L3 route-pin summary field (present for layer "l3").
  route_count?: number | null;
  status: WiringConnectionState;
  intended?: "ACTIVE" | "RELEASED";
  attempts: number;
  last_error: string | null;
  retryable: boolean;
  created_at: string | null;
  released_at: string | null;
}

// GET /reservations/{id}/wiring-status: the reservation's layered applied
// wiring state plus its wiring-state markers. A reservation with no rows and no
// state row is the empty/pre-apply case (physical-only pre-P3b, dynamic-only, or
// no fork edits): an empty connections list, null version, not frozen.
export interface WiringStatusResponse {
  reservation_id: string;
  last_applied_fork_version: number | null;
  frozen: boolean;
  connections: WiringConnectionStatus[];
}

// The outcome of reattempting (or classifying) one FAILED connection on retry.
// "reconnected" a build succeeded, "released" a release succeeded, "superseded" a
// release a newer build already made redundant (no driver call), "still_failed" a
// reattempt failed again, "not_retryable" a pinned unresolvable intent (recovery is a
// re-save), "frozen" a build refused on an ended reservation (release-direction rows
// still retry; ADR 0009 phase 3, issue #369).
export type WiringRetryOutcomeKind =
  | "reconnected"
  | "released"
  | "superseded"
  | "still_failed"
  | "not_retryable"
  | "frozen";

// Layered since ADR 0009 phases 4-5 (issue #416): `layer` is "l1" for a cross-connect row
// (port_a/port_b/physical_connection_id populated), "l2" for a VLAN membership row
// (port/vlan_assignment_id/vlan populated, port_a/port_b null), or "l3" for a per-switch
// route pin (route_count populated, no port fields). The layer-specific fields are nullable
// so a row omits the ones that do not apply; the outcome vocabulary is unchanged.
export interface WiringRetryOutcome {
  id: string;
  switch_device_id: string;
  layer: "l1" | "l2" | "l3";
  // L1 cross-connect fields (present for layer "l1").
  port_a: string | null;
  port_b: string | null;
  physical_connection_id: string | null;
  // L2 membership fields (present for layer "l2").
  port?: string | null;
  vlan_assignment_id?: string | null;
  vlan?: number | null;
  // L3 route-pin summary field (present for layer "l3").
  route_count?: number | null;
  outcome: WiringRetryOutcomeKind;
  status: WiringConnectionState;
  attempts: number;
  last_error: string | null;
}

// POST /reservations/{id}/wiring/retry: per-connection outcomes of a manual
// retry of every hardware-retryable FAILED row of one reservation.
export interface WiringRetryResponse {
  reservation_id: string;
  results: WiringRetryOutcome[];
}
