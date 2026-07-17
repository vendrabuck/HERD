import { useAllDeviceNames } from "@/api/inventory";
import { useReservationWiringStatus, useRetryReservationWiring } from "@/api/reservations";
import type { WiringConnectionStatus } from "@/types/reservation.types";

// Per-connection L1 wiring status (ADR 0007, issue #345 P3b phase 5). After a
// fork save reconciles the intended wiring, execution applies each L1
// cross-connect connection-by-connection; this panel surfaces the applied state
// (ACTIVE / RELEASED / FAILED), the reservation-level last-applied fork version
// and frozen marker, and, for an ACTIVE reservation, a manual retry of the
// hardware-retryable FAILED rows. FAILED rows that are not hardware-retryable
// (an unresolvable or not-a-simple-chain intent) explain that a fork re-save is
// the recovery, not a retry (ADR 0007 Decision 5/6).

interface Props {
  reservationId: string;
  // The retry endpoint 409s for a non-ACTIVE reservation, so the button renders
  // only while the reservation is ACTIVE; an ended reservation shows the panel
  // read-only.
  active: boolean;
}

const STATUS_COLORS: Record<string, string> = {
  ACTIVE: "bg-green-100 text-green-800",
  RELEASED: "bg-gray-100 text-gray-600",
  FAILED: "bg-red-200 text-red-900",
};

function ConnectionRow({
  conn,
  deviceNames,
}: {
  conn: WiringConnectionStatus;
  deviceNames: Map<string, string> | undefined;
}) {
  const switchName = deviceNames?.get(conn.switch_device_id) ?? conn.switch_device_id.slice(0, 8);
  const isFailed = conn.status === "FAILED";
  const notRetryable = isFailed && !conn.retryable;

  return (
    <div className="border border-gray-200 rounded overflow-hidden">
      <div className="flex items-center gap-2 px-3 py-2 bg-gray-50 border-b border-gray-100">
        <span className="text-sm font-medium text-gray-800">{switchName}</span>
        <span className="text-xs font-mono text-gray-500">
          {conn.port_a} to {conn.port_b}
        </span>
        <span
          className={`ml-auto text-xs px-1.5 py-0.5 rounded font-medium ${
            STATUS_COLORS[conn.status] ?? "bg-gray-100 text-gray-600"
          }`}
        >
          {conn.status}
        </span>
      </div>
      <div className="flex items-center gap-3 px-3 py-1.5 text-xs text-gray-500">
        <span>
          Attempts: <span className="font-medium text-gray-700">{conn.attempts}</span>
        </span>
        {conn.physical_connection_id && (
          <span className="font-mono">cable {conn.physical_connection_id.slice(0, 8)}</span>
        )}
      </div>
      {isFailed && conn.last_error && (
        <p className="px-3 pb-2 text-xs text-red-600 break-words">{conn.last_error}</p>
      )}
      {notRetryable && (
        <p className="px-3 pb-2 text-xs text-amber-700">
          This connection cannot be reapplied to hardware as recorded. Recovery is to re-save
          the fork wiring, which re-resolves the path against current inventory, then retry.
        </p>
      )}
    </div>
  );
}

export function ReservationWiringTab({ reservationId, active }: Props) {
  const { data, isLoading, isError } = useReservationWiringStatus(reservationId);
  const { data: deviceNames } = useAllDeviceNames();
  const retry = useRetryReservationWiring();

  if (isLoading) {
    return <p className="text-xs text-gray-400 text-center py-8">Loading wiring status...</p>;
  }

  if (isError) {
    return <p className="text-sm text-red-500 text-center py-8">Failed to load wiring status.</p>;
  }

  const connections = data?.connections ?? [];

  if (connections.length === 0) {
    return (
      <p className="text-sm text-gray-400 text-center py-8">
        No per-connection wiring for this reservation.
      </p>
    );
  }

  // A single retry button reattempts every hardware-retryable FAILED row at once,
  // matching the endpoint's all-FAILED semantics. It renders only for an ACTIVE
  // reservation with at least one retryable FAILED row (the endpoint 409s for a
  // non-ACTIVE reservation, and a retry with nothing to retry is a no-op).
  const hasRetryable = connections.some((c) => c.status === "FAILED" && c.retryable);
  const failedCount = connections.filter((c) => c.status === "FAILED").length;

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-3 text-xs text-gray-500">
        <span>
          Applied fork version:{" "}
          <span className="font-medium text-gray-700">
            {data?.last_applied_fork_version ?? "none"}
          </span>
        </span>
        {data?.frozen && (
          <span className="px-1.5 py-0.5 rounded bg-gray-200 text-gray-700 font-medium">
            Frozen
          </span>
        )}
        {failedCount > 0 && (
          <span className="px-1.5 py-0.5 rounded bg-red-100 text-red-700 font-medium">
            {failedCount} failed
          </span>
        )}
        {active && hasRetryable && (
          <button
            onClick={() => retry.mutate(reservationId)}
            disabled={retry.isPending}
            className="ml-auto text-xs px-2.5 py-1.5 rounded bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50"
          >
            {retry.isPending ? "Retrying..." : "Retry failed"}
          </button>
        )}
      </div>

      <div className="space-y-2">
        {connections.map((conn) => (
          <ConnectionRow key={conn.id} conn={conn} deviceNames={deviceNames} />
        ))}
      </div>
    </div>
  );
}
