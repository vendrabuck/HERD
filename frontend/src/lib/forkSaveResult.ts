import type { ForkSkippedConstrainedEdge } from "@/types/reservation.types";

// Display helpers for the fork save result toast (ForkSaveResultToast).

export function shortId(id: string): string {
  return id.length > 8 ? id.slice(0, 8) : id;
}

// One line the save could not wire on its chosen ports, as
// "<device> <port> to <device> <port>" (issue #1007). A device missing from
// the label map falls back to its short id; a side with no chosen port reads
// "any port".
export function skippedEdgeText(
  edge: ForkSkippedConstrainedEdge,
  deviceLabels: Record<string, string> = {},
): string {
  const side = (deviceId: string, port: string | null) =>
    `${deviceLabels[deviceId] ?? shortId(deviceId)} ${port ?? "any port"}`;
  return `${side(edge.source_device_id, edge.source_port_name)} to ${side(
    edge.target_device_id,
    edge.target_port_name,
  )}`;
}
