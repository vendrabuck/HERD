import { useAllDeviceNames } from "@/api/inventory";
import { useReservationWiringStatus, useRetryReservationWiring } from "@/api/reservations";
import type { WiringConnectionStatus, WiringLayer } from "@/types/reservation.types";

// Layered per-connection wiring status (ADR 0007, issue #345 P3b; layered by
// ADR 0009 phase 8, issue #416). After a fork save reconciles the intended
// wiring, execution applies each layer connection-by-connection; this panel
// groups the applied rows by layer (L1 switch cross-connects, L2 VLAN
// memberships, L3 route pins) and surfaces each row's state (ACTIVE / RELEASED
// / FAILED), the reservation-level last-applied fork version and frozen marker,
// and, for an ACTIVE reservation, a manual retry of the hardware-retryable
// FAILED rows across every layer. FAILED rows that are not hardware-retryable
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

const LAYER_ORDER: WiringLayer[] = ["l1", "l2", "l3"];

const LAYER_HEADINGS: Record<WiringLayer, string> = {
  l1: "L1 cross-connects",
  l2: "L2 VLAN memberships",
  l3: "L3 route pins",
};

// A pre-phase-8 backend omits `layer`; those rows are L1 cross-connects.
function rowLayer(conn: WiringConnectionStatus): WiringLayer {
  return conn.layer ?? "l1";
}

// The row's layer-specific identity line: an L1 row names its port pair, an L2
// row its member port and allocated VLAN (null while the allocation is parked),
// an L3 row its pinned-route count.
function rowDetail(conn: WiringConnectionStatus): string {
  const layer = rowLayer(conn);
  if (layer === "l2") {
    const vlan = conn.vlan != null ? `VLAN ${conn.vlan}` : "VLAN unassigned";
    return `port ${conn.port ?? "?"} ${vlan}`;
  }
  if (layer === "l3") {
    const n = conn.route_count ?? 0;
    return n === 1 ? "1 route" : `${n} routes`;
  }
  return `${conn.port_a} to ${conn.port_b}`;
}

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
        <span className="text-xs font-mono text-gray-500">{rowDetail(conn)}</span>
        {isFailed && conn.intended === "RELEASED" && (
          <span className="text-xs px-1.5 py-0.5 rounded bg-amber-100 text-amber-800 font-medium">
            release pending
          </span>
        )}
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

  // A single retry button reattempts every hardware-retryable FAILED row across
  // all layers at once, matching the endpoint's all-FAILED semantics. It renders
  // only for an ACTIVE reservation with at least one retryable FAILED row (the
  // endpoint 409s for a non-provisioned reservation, and a retry with nothing to
  // retry is a no-op).
  const hasRetryable = connections.some((c) => c.status === "FAILED" && c.retryable);
  const failedCount = connections.filter((c) => c.status === "FAILED").length;

  // Group rows by layer, in fixed L1/L2/L3 order; a section renders only when it
  // has rows, so an L1-only reservation still reads as a single flat list under
  // one heading.
  const sections = LAYER_ORDER.map((layer) => ({
    layer,
    rows: connections.filter((c) => rowLayer(c) === layer),
  })).filter((s) => s.rows.length > 0);

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

      {sections.map((section) => (
        <div key={section.layer} className="space-y-2">
          <p className="text-xs font-medium text-gray-500 uppercase tracking-wide">
            {LAYER_HEADINGS[section.layer]}
          </p>
          {section.rows.map((conn) => (
            <ConnectionRow key={conn.id} conn={conn} deviceNames={deviceNames} />
          ))}
        </div>
      ))}
    </div>
  );
}
