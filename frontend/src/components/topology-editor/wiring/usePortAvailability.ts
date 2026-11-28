import { useMemo } from "react";
import { usePorts } from "@/api/ports";
import { useDeviceConnections } from "@/api/connections";
import { computeCabledNames } from "./portAvailability";
import { loadState, noEntries } from "@/lib/loadState";
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

/**
 * A side whose ports or cabling could not be loaded (issue #1320): the column
 * shows `LoadError` for `what` with Retry instead of its ports, so a failed
 * ports read never reads "No ports configured" and a failed connections read
 * never shows every port as free to cable.
 */
export interface PortSideLoadError {
  what: string;
  error: unknown;
  retry: () => void;
  retrying: boolean;
}

export interface PortSideData {
  ports: Port[];
  isLoading: boolean;
  // Port names with a registered physical connection (see portAvailability).
  cabled: Set<string>;
  // Set when this side's ports or its connections failed to load (the ports
  // failure wins when both did); null otherwise.
  loadError: PortSideLoadError | null;
}

export interface PortAvailabilityResult {
  source: PortSideData;
  target: PortSideData;
  // True while either side's physical cabling is not known: its fetch is in
  // flight, or it failed with nothing loaded (issue #1320). Availability must
  // never be computed against a connections list that is empty only because
  // it never arrived (issue #517 review item 6), so callers gate interaction
  // on this.
  connectionsUnknown: boolean;
}

/** The slice of a TanStack Query result this hook reads. */
interface SideQuery<T> {
  data: T | undefined;
  isLoading: boolean;
  isError: boolean;
  isFetching: boolean;
  error: unknown;
  refetch: () => unknown;
}

/** True when the query failed and holds nothing (loadState's `error`). */
function failed<T>(query: SideQuery<T[]>): boolean {
  return loadState(query, noEntries) === "error";
}

/**
 * The side's load error, memoized on the fields it reads so the side object
 * (and every memo chain downstream of it) stays referentially stable while
 * nothing about the failure changes.
 */
function useSideLoadError(
  ports: SideQuery<Port[]>,
  connections: SideQuery<Connection[]>,
): PortSideLoadError | null {
  const portsFailed = failed(ports);
  const connectionsFailed = failed(connections);
  const portsError = ports.error;
  const portsFetching = ports.isFetching;
  const portsRefetch = ports.refetch;
  const connectionsError = connections.error;
  const connectionsFetching = connections.isFetching;
  const connectionsRefetch = connections.refetch;
  return useMemo(() => {
    if (portsFailed) {
      return {
        what: "this device's ports",
        error: portsError,
        retry: () => void portsRefetch(),
        retrying: portsFetching,
      };
    }
    if (connectionsFailed) {
      return {
        what: "this device's connections",
        error: connectionsError,
        retry: () => void connectionsRefetch(),
        retrying: connectionsFetching,
      };
    }
    return null;
  }, [
    portsFailed,
    portsError,
    portsFetching,
    portsRefetch,
    connectionsFailed,
    connectionsError,
    connectionsFetching,
    connectionsRefetch,
  ]);
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
  const sourcePortsQuery = usePorts(sourceDeviceId);
  const targetPortsQuery = usePorts(targetDeviceId);
  const sourceConnsQuery = useDeviceConnections(sourceDeviceId);
  const targetConnsQuery = useDeviceConnections(targetDeviceId);
  const { data: sourcePorts, isLoading: sourceLoading } = sourcePortsQuery;
  const { data: targetPorts, isLoading: targetLoading } = targetPortsQuery;
  const { data: sourceConns, isLoading: sourceConnsLoading } = sourceConnsQuery;
  const { data: targetConns, isLoading: targetConnsLoading } = targetConnsQuery;

  const sourceCabled = useMemo(
    () => computeCabledNames(sourceConns ?? NO_CONNECTIONS, sourceDeviceId),
    [sourceConns, sourceDeviceId],
  );
  const targetCabled = useMemo(
    () => computeCabledNames(targetConns ?? NO_CONNECTIONS, targetDeviceId),
    [targetConns, targetDeviceId],
  );

  const sourceLoadError = useSideLoadError(sourcePortsQuery, sourceConnsQuery);
  const targetLoadError = useSideLoadError(targetPortsQuery, targetConnsQuery);

  const source: PortSideData = useMemo(
    () => ({
      ports: sourcePorts ?? NO_PORTS,
      isLoading: sourceLoading,
      cabled: sourceCabled,
      loadError: sourceLoadError,
    }),
    [sourcePorts, sourceLoading, sourceCabled, sourceLoadError],
  );
  const target: PortSideData = useMemo(
    () => ({
      ports: targetPorts ?? NO_PORTS,
      isLoading: targetLoading,
      cabled: targetCabled,
      loadError: targetLoadError,
    }),
    [targetPorts, targetLoading, targetCabled, targetLoadError],
  );
  // A side whose connections failed reads as unknown, never as "nothing
  // cabled": every port would otherwise show, and act, as free to cable.
  const connectionsUnknown =
    sourceConnsLoading ||
    targetConnsLoading ||
    failed(sourceConnsQuery) ||
    failed(targetConnsQuery);

  return useMemo(
    () => ({ source, target, connectionsUnknown }),
    [source, target, connectionsUnknown],
  );
}
