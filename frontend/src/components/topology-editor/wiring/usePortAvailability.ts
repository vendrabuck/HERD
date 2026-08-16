import { useMemo } from "react";
import { usePorts } from "@/api/ports";
import { useDeviceConnections } from "@/api/connections";
import { computeCabledNames } from "./portAvailability";
import type { Port } from "@/types/port.types";
import type { Connection } from "@/types/connection.types";

// Module-level, reused across every render and every call (issue #517
// review round 3 item 1): `const { data: x = [] } = usePorts(...)` fabricates
// a BRAND NEW `[]` on every single render for as long as `data` is
// `undefined` (before the query resolves, or on error), because a
// destructuring default re-evaluates its expression each time, unlike a
// module-level constant. That fresh reference flows into portIndexBySide's
// useMemo in WiringDialog, which then differs every render, which reruns the
// line-geometry useLayoutEffect every render forever: a real, confirmed
// "Maximum update depth exceeded" crash on the very first mount, before any
// query has resolved. Every "no data yet" case must resolve to the SAME
// object reference.
const NO_PORTS: Port[] = [];
const NO_CONNECTIONS: Connection[] = [];

export interface PortSideData {
  ports: Port[];
  isLoading: boolean;
  // Port names with a registered physical connection (see portAvailability).
  cabled: Set<string>;
}

export interface PortAvailabilityResult {
  source: PortSideData;
  target: PortSideData;
  // True while either side's physical-cabling fetch is in flight: availability
  // must never be computed against a still-empty connections list (issue #517
  // review item 6), so callers gate interaction on this.
  connectionsLoading: boolean;
}

/**
 * Shared ports+connections+availability derivation for both wiring surfaces
 * (WiringDialog and QuickConnectPopover), issue #517 review item 10.
 *
 * The whole per-side object and the overall result are memoized (review item
 * 10b) so a caller downstream (WiringDialog builds several useMemo/
 * useLayoutEffect chains off `source`/`target`) gets a referentially stable
 * value across renders where nothing actually changed. This only holds if
 * usePorts/useDeviceConnections themselves return stable references for
 * unchanged data, which TanStack Query does via structural sharing; a test
 * mock that fabricates a fresh `[]` on every call breaks that guarantee same
 * as a real, buggy query client would, so test fixtures reuse one stable
 * empty-array constant instead of inlining `[]` per mock call.
 */
export function usePortAvailability(
  sourceDeviceId: string,
  targetDeviceId: string,
): PortAvailabilityResult {
  const { data: sourcePorts, isLoading: sourceLoading } = usePorts(sourceDeviceId);
  const { data: targetPorts, isLoading: targetLoading } = usePorts(targetDeviceId);
  const { data: sourceConns, isLoading: sourceConnsLoading } = useDeviceConnections(sourceDeviceId);
  const { data: targetConns, isLoading: targetConnsLoading } = useDeviceConnections(targetDeviceId);

  const sourceCabled = useMemo(
    () => computeCabledNames(sourceConns ?? NO_CONNECTIONS, sourceDeviceId),
    [sourceConns, sourceDeviceId],
  );
  const targetCabled = useMemo(
    () => computeCabledNames(targetConns ?? NO_CONNECTIONS, targetDeviceId),
    [targetConns, targetDeviceId],
  );

  const source: PortSideData = useMemo(
    () => ({ ports: sourcePorts ?? NO_PORTS, isLoading: sourceLoading, cabled: sourceCabled }),
    [sourcePorts, sourceLoading, sourceCabled],
  );
  const target: PortSideData = useMemo(
    () => ({ ports: targetPorts ?? NO_PORTS, isLoading: targetLoading, cabled: targetCabled }),
    [targetPorts, targetLoading, targetCabled],
  );
  const connectionsLoading = sourceConnsLoading || targetConnsLoading;

  return useMemo(
    () => ({ source, target, connectionsLoading }),
    [source, target, connectionsLoading],
  );
}
