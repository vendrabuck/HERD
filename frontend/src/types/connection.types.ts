export interface Connection {
  id: string;
  device_a_id: string;
  port_a: string;
  device_b_id: string;
  port_b: string;
  connection_type: string;
  notes: string | null;
  created_by: string;
  created_at: string;
  modified_by: string | null;
  updated_at: string | null;
}

// A transit hop through a device outside a non-admin caller's device-group
// visibility comes back redacted (issue #763): device_id null, hidden true, and
// no port names. The hop keeps its place in the path, so hop_count and
// reachability are what they always were; only the identity is withheld. Any
// consumer that looks a hop up by device_id must tolerate null.
export interface PathHop {
  device_id: string | null;
  port_in: string | null;
  port_out: string | null;
  hidden?: boolean;
}

export interface PathfindResponse {
  reachable: boolean;
  hop_count: number;
  paths: PathHop[][];
}

export interface PathfindBatchResult extends PathfindResponse {
  source_device_id: string;
  target_device_id: string;
  // Per-pair refusal (issue #763): set when the pair names a device the caller
  // cannot see, null on every ordinary result, so an unreachable pair stays
  // distinguishable from a refused one.
  error?: string | null;
}

export interface PathfindBatchResponse {
  results: PathfindBatchResult[];
}

export interface ConnectionCreate {
  device_a_id: string;
  port_a: string;
  device_b_id: string;
  port_b: string;
  connection_type?: string;
  notes?: string;
}

export type BulkConnectionRowStatus = "created" | "rejected";

/**
 * One row of a POST /cabling/connections/bulk response, positionally keyed to
 * the request's `items` array by `index`.
 */
export interface BulkConnectionRow {
  index: number;
  status: BulkConnectionRowStatus;
  connection_id: string | null;
  error: string | null;
}

/**
 * The bulk create result. A row-level rejection is NOT an HTTP error: the
 * endpoint answers 200 even when `rejected` is non-zero, so a caller that
 * only checks the HTTP status will report success for a batch that partly
 * failed. Always read `created`, `rejected`, and `rows`.
 */
export interface BulkConnectionResult {
  created: number;
  rejected: number;
  rows: BulkConnectionRow[];
}
