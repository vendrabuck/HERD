import type { Port } from "@/types/port.types";

/**
 * Shared substring port-name filter (issue #517 review item 7): PortColumn
 * uses this to decide what it renders, and WiringDialog's "Connect 1:1 in
 * order" uses the SAME function so it only pairs ports the user can
 * currently see, instead of secretly reaching past an active filter.
 */
export function filterPorts(ports: Port[], filterText: string): Port[] {
  const needle = filterText.trim().toLowerCase();
  if (!needle) return ports;
  return ports.filter((p) => p.name.toLowerCase().includes(needle));
}
