import type {
  BulkConnectionResult,
  Connection,
  ConnectionCreate,
} from "@/types/connection.types";

/**
 * Pure staging arithmetic for the admin multi-connect dialog, kept out of the
 * component so the conflict rules and the partial-success bookkeeping are
 * testable without rendering anything.
 */

/** One staged (not yet created) cable between the A-side and B-side devices. */
export interface StagedLine {
  id: string;
  sourcePortId: string;
  sourcePortName: string;
  targetPortId: string;
  targetPortName: string;
  // The same pair is already registered as a connection. A WARNING only: the
  // backend deliberately permits duplicates, so the admin may confirm anyway.
  duplicate: boolean;
  // Either end already carries some registered cable (not necessarily to the
  // counterpart). Also warning-only, and rendered by PortColumn's CABLED tag.
  sourceCabled: boolean;
  targetCabled: boolean;
  // Set from a rejected bulk row after a partially failed submit.
  error: string | null;
}

/** Order-sensitive key for an (A-side port, B-side port) pair. */
export function pairKey(portA: string, portB: string): string {
  return JSON.stringify([portA, portB]);
}

/** Composite key so a device selected on BOTH sides shares one used-port set. */
export function portKey(deviceId: string | undefined, portId: string): string {
  return `${deviceId ?? ""}::${portId}`;
}

/**
 * Pair keys for connections that already join the two selected devices,
 * normalized to (A-side port, B-side port) order.
 */
export function existingPairKeys(
  connections: Connection[],
  deviceAId: string,
  deviceBId: string,
): Set<string> {
  const keys = new Set<string>();
  for (const conn of connections) {
    // Independent checks, not else-if: when the SAME device is picked on both
    // sides (a loopback cable patching two ports on one box), a single row is
    // a duplicate candidate in both orientations.
    if (conn.device_a_id === deviceAId && conn.device_b_id === deviceBId) {
      keys.add(pairKey(conn.port_a, conn.port_b));
    }
    if (conn.device_a_id === deviceBId && conn.device_b_id === deviceAId) {
      keys.add(pairKey(conn.port_b, conn.port_a));
    }
  }
  return keys;
}

/**
 * The bulk request body's items, in staging order. The batch-level type and
 * notes are a UI concept only: they are stamped onto every item.
 */
export function buildBulkItems(
  lines: StagedLine[],
  deviceAId: string,
  deviceBId: string,
  connectionType: string,
  notes: string,
): ConnectionCreate[] {
  const type = connectionType.trim() || "ethernet";
  const trimmedNotes = notes.trim();
  return lines.map((line) => {
    const item: ConnectionCreate = {
      device_a_id: deviceAId,
      port_a: line.sourcePortName,
      device_b_id: deviceBId,
      port_b: line.targetPortName,
      connection_type: type,
    };
    if (trimmedNotes) item.notes = trimmedNotes;
    return item;
  });
}

export interface BulkOutcome {
  /** Lines that were NOT created, carrying the server's reason. */
  remaining: StagedLine[];
  /** Null only when every line was created. */
  summary: string | null;
}

/**
 * Fold a bulk response back onto the staged lines: created lines drop out,
 * everything else stays staged with its reason attached so the admin can fix
 * and retry only the failures.
 *
 * Rows are matched positionally by `index`, the contract the request's `items`
 * array establishes. A line with no matching row is deliberately KEPT, not
 * dropped: an absent row is not evidence of creation, and silently discarding
 * it would report a connection as done that nobody can show exists.
 */
export function applyBulkResult(lines: StagedLine[], result: BulkConnectionResult): BulkOutcome {
  const rowByIndex = new Map(result.rows.map((row) => [row.index, row]));
  const remaining: StagedLine[] = [];
  lines.forEach((line, index) => {
    const row = rowByIndex.get(index);
    if (row?.status === "created") return;
    const error = row
      ? (row.error ?? "Rejected without a reason")
      : "No result returned for this row";
    remaining.push({ ...line, error });
  });

  if (remaining.length === 0) return { remaining, summary: null };
  const noun = lines.length === 1 ? "connection" : "connections";
  return {
    remaining,
    summary:
      `Created ${result.created} of ${lines.length} ${noun}, ${result.rejected} rejected. ` +
      "Fix the flagged lines and retry.",
  };
}
