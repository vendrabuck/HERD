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

export interface PathHop {
  device_id: string;
  port_in: string | null;
  port_out: string | null;
}

export interface PathfindResponse {
  reachable: boolean;
  hop_count: number;
  paths: PathHop[][];
}

export interface PathfindBatchResult extends PathfindResponse {
  source_device_id: string;
  target_device_id: string;
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
