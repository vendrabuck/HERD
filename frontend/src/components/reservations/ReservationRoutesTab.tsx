import { Fragment, useMemo } from "react";
import { useAllDeviceNames } from "@/api/inventory";
import { usePathfindPairs } from "@/api/connections";
import type { DevicePair } from "@/api/connections";
import type { PathHop } from "@/types/connection.types";

interface Props {
  deviceIds: string[];
}

function RouteHops({
  path,
  deviceNameMap,
}: {
  path: PathHop[];
  deviceNameMap: Map<string, string> | undefined;
}) {
  return (
    <div className="flex items-center gap-1 overflow-x-auto py-2 px-1">
      {path.map((hop, idx) => (
        <Fragment key={idx}>
          {idx > 0 && (
            <div className="flex items-center gap-0.5 shrink-0 text-xs text-gray-400 font-mono">
              <span>{path[idx - 1].port_out}</span>
              <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M9 5l7 7-7 7" />
              </svg>
              <span>{hop.port_in}</span>
            </div>
          )}
          <div className="shrink-0 px-2.5 py-1.5 rounded border border-gray-200 bg-gray-50 text-center">
            <span className="text-xs font-medium text-gray-800 whitespace-nowrap">
              {deviceNameMap?.get(hop.device_id) ?? hop.device_id.slice(0, 8)}
            </span>
          </div>
        </Fragment>
      ))}
    </div>
  );
}

function RouteCard({
  sourceId,
  targetId,
  reachable,
  hopCount,
  paths,
  deviceNameMap,
}: {
  sourceId: string;
  targetId: string;
  reachable: boolean;
  hopCount: number;
  paths: PathHop[][];
  deviceNameMap: Map<string, string> | undefined;
}) {
  const sourceName = deviceNameMap?.get(sourceId) ?? sourceId.slice(0, 8);
  const targetName = deviceNameMap?.get(targetId) ?? targetId.slice(0, 8);
  const routeCount = paths.length;

  return (
    <div className="border border-gray-200 rounded overflow-hidden">
      <div className="flex items-center gap-2 px-4 py-2 bg-gray-50 border-b border-gray-100">
        <span className="text-sm font-medium text-gray-800">{sourceName}</span>
        <svg className="w-4 h-4 text-gray-400 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M17 8l4 4m0 0l-4 4m4-4H3" />
        </svg>
        <span className="text-sm font-medium text-gray-800">{targetName}</span>
        {reachable ? (
          <span className="ml-auto text-xs px-1.5 py-0.5 rounded bg-green-100 text-green-700 font-medium">
            {routeCount > 1 ? `${routeCount} routes, ` : ""}
            {hopCount} {hopCount === 1 ? "hop" : "hops"}
          </span>
        ) : (
          <span className="ml-auto text-xs px-1.5 py-0.5 rounded bg-red-100 text-red-700 font-medium">
            Unreachable
          </span>
        )}
      </div>
      {reachable ? (
        <div className="px-3 py-1 divide-y divide-gray-100">
          {paths.map((path, idx) => (
            <RouteHops key={idx} path={path} deviceNameMap={deviceNameMap} />
          ))}
        </div>
      ) : (
        <p className="text-xs text-gray-400 px-4 py-3">No physical path found between these devices.</p>
      )}
    </div>
  );
}

export function ReservationRoutesTab({ deviceIds }: Props) {
  const { data: deviceNameMap } = useAllDeviceNames();

  const pairs = useMemo(() => {
    const result: DevicePair[] = [];
    for (let i = 0; i < deviceIds.length; i++) {
      for (let j = i + 1; j < deviceIds.length; j++) {
        result.push({ sourceDeviceId: deviceIds[i], targetDeviceId: deviceIds[j] });
      }
    }
    return result;
  }, [deviceIds]);

  const { data: routeMap, isLoading, isError } = usePathfindPairs(pairs);

  if (deviceIds.length < 2) {
    return <p className="text-sm text-gray-400 text-center py-8">Routes require at least 2 devices.</p>;
  }

  if (isLoading) {
    return <p className="text-xs text-gray-400 text-center py-8">Discovering routes...</p>;
  }

  if (isError) {
    return <p className="text-sm text-red-500 text-center py-8">Failed to load routes.</p>;
  }

  const reachableRoutes = pairs.filter((p) => {
    const result = routeMap?.get(`${p.sourceDeviceId}::${p.targetDeviceId}`);
    return result?.reachable;
  });

  const unreachableRoutes = pairs.filter((p) => {
    const result = routeMap?.get(`${p.sourceDeviceId}::${p.targetDeviceId}`);
    return result && !result.reachable;
  });

  if (reachableRoutes.length === 0 && unreachableRoutes.length === 0) {
    return <p className="text-sm text-gray-400 text-center py-8">No physical routes found between reservation devices.</p>;
  }

  return (
    <div className="space-y-3">
      {reachableRoutes.map((p) => {
        const result = routeMap!.get(`${p.sourceDeviceId}::${p.targetDeviceId}`)!;
        return (
          <RouteCard
            key={`${p.sourceDeviceId}::${p.targetDeviceId}`}
            sourceId={p.sourceDeviceId}
            targetId={p.targetDeviceId}
            reachable={result.reachable}
            hopCount={result.hop_count}
            paths={result.paths}
            deviceNameMap={deviceNameMap}
          />
        );
      })}
      {unreachableRoutes.map((p) => (
        <RouteCard
          key={`${p.sourceDeviceId}::${p.targetDeviceId}`}
          sourceId={p.sourceDeviceId}
          targetId={p.targetDeviceId}
          reachable={false}
          hopCount={0}
          paths={[]}
          deviceNameMap={deviceNameMap}
        />
      ))}
    </div>
  );
}
