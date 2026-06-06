import { useQuery, useMutation, useQueryClient, keepPreviousData } from "@tanstack/react-query";
import type { PathfindResponse, Connection, ConnectionCreate } from "@/types/connection.types";
import type { PaginatedResponse } from "@/types/pagination.types";
import apiClient from "./client";

async function fetchPaginatedConnections(
  skip = 0,
  limit = 50,
  deviceId?: string,
): Promise<PaginatedResponse<Connection>> {
  const params: Record<string, string | number> = { skip, limit };
  if (deviceId) params.device_id = deviceId;
  const resp = await apiClient.get<PaginatedResponse<Connection>>("/cabling/connections", { params });
  return resp.data;
}

async function createConnection(data: ConnectionCreate): Promise<Connection> {
  const resp = await apiClient.post<Connection>("/cabling/connections", data);
  return resp.data;
}

async function deleteConnection(id: string): Promise<void> {
  await apiClient.delete(`/cabling/connections/${id}`);
}

export function usePaginatedConnections(skip = 0, limit = 50, deviceId?: string) {
  return useQuery({
    queryKey: ["connections", "paginated", skip, limit, deviceId],
    queryFn: () => fetchPaginatedConnections(skip, limit, deviceId),
    placeholderData: keepPreviousData,
  });
}

export function useDeviceConnections(deviceId: string | undefined) {
  return useQuery({
    queryKey: ["connections", "device", deviceId],
    queryFn: () => fetchPaginatedConnections(0, 500, deviceId!).then((r) => r.items),
    enabled: !!deviceId,
  });
}

export function useMultiDeviceConnections(deviceIds: string[]) {
  const key = deviceIds.slice().sort().join(",");
  return useQuery({
    queryKey: ["connections", "multi-device", key],
    queryFn: async () => {
      const all: Connection[] = [];
      const seen = new Set<string>();
      for (const id of deviceIds) {
        const resp = await fetchPaginatedConnections(0, 500, id);
        for (const conn of resp.items) {
          if (!seen.has(conn.id)) {
            seen.add(conn.id);
            all.push(conn);
          }
        }
      }
      return all;
    },
    enabled: deviceIds.length > 0,
  });
}

export function useCreateConnection() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: createConnection,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["connections"] });
    },
  });
}

export function useDeleteConnection() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: deleteConnection,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["connections"] });
    },
  });
}

async function findPath(
  sourceDeviceId: string,
  targetDeviceId: string,
): Promise<PathfindResponse> {
  const resp = await apiClient.post<PathfindResponse>("/cabling/pathfind", {
    source_device_id: sourceDeviceId,
    target_device_id: targetDeviceId,
  });
  return resp.data;
}

export function usePathfind() {
  return useMutation({
    mutationFn: ({ sourceDeviceId, targetDeviceId }: {
      sourceDeviceId: string;
      targetDeviceId: string;
    }) => findPath(sourceDeviceId, targetDeviceId),
  });
}

export interface DevicePair {
  sourceDeviceId: string;
  targetDeviceId: string;
}

export function usePathfindPairs(pairs: DevicePair[]) {
  const key = pairs
    .map((p) => `${p.sourceDeviceId}:${p.targetDeviceId}`)
    .sort()
    .join(",");

  return useQuery({
    queryKey: ["pathfind", "pairs", key],
    queryFn: async () => {
      const results = await Promise.all(
        pairs.map(async (p) => {
          try {
            const resp = await findPath(p.sourceDeviceId, p.targetDeviceId);
            return { pair: p, result: resp };
          } catch {
            return {
              pair: p,
              result: { reachable: false, hop_count: 0, paths: [] } as PathfindResponse,
            };
          }
        }),
      );
      const map = new Map<string, PathfindResponse>();
      for (const { pair, result } of results) {
        map.set(`${pair.sourceDeviceId}::${pair.targetDeviceId}`, result);
      }
      return map;
    },
    enabled: pairs.length > 0,
    staleTime: 2 * 60 * 1000,
  });
}
