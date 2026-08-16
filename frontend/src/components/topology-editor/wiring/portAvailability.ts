import type { Connection } from "@/types/connection.types";

// Single shared instance (issue #517 review round 3 item 12.9): both
// WiringDialog and QuickConnectPopover default their existingWired*PortIds
// props to this when the caller (TopologyEditorPage) has nothing to pass,
// e.g. because no pendingConnection exists yet.
export const EMPTY_ID_SET: ReadonlySet<string> = new Set<string>();

/**
 * Port names with ANY registered physical connection (from
 * useDeviceConnections, the admin cabling resource), matched by device id
 * exactly as ConnectionModal's original derivation did.
 *
 * Restored to the OLD ConnectionModal semantics after review (issue #517):
 * an earlier version of this dialog blocked a port that was cabled to any
 * device OTHER than the dialog's counterpart, reasoning that a port with an
 * unrelated real cable couldn't sensibly take a second logical wire. That
 * reasoning does not hold for HERD's actual topology: DUT ports are patched
 * into L1 edge switches, so DUT-to-DUT canvas edges are cabled through
 * intermediate hardware, never directly to each other. Under that "elsewhere
 * blocks" rule the dialog refused every properly cabled port on seeded
 * inventory and only let users wire unpatched ones, exactly backwards.
 *
 * A port with ANY connection row is selectable and marks the emitted edge
 * portsCabled true (pathfind, run separately by the editor, resolves the
 * actual hop path and is the real reachability authority). A port with no
 * connection row is still selectable too, landing portsCabled false, the
 * existing soft "uncabled port" warning on the canvas. Nothing is blocked by
 * cabling state; the only thing this dialog blocks is a port already used by
 * another line in the current session (tracked separately, see WiringDialog).
 */
export function computeCabledNames(connections: Connection[], deviceId: string): Set<string> {
  const cabled = new Set<string>();
  for (const conn of connections) {
    // Independent checks, not else-if (issue #517 review item 5): a
    // same-device loopback connection (device_a_id === device_b_id, a real
    // physical cable patching two ports on the same box to each other) has
    // BOTH port_a and port_b belonging to this device. An else-if would only
    // ever record port_a for such a row, silently dropping port_b from the
    // cabled set.
    if (conn.device_a_id === deviceId) {
      cabled.add(conn.port_a);
    }
    if (conn.device_b_id === deviceId) {
      cabled.add(conn.port_b);
    }
  }
  return cabled;
}
