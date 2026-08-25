import { useQuery, useMutation, useQueryClient, keepPreviousData } from "@tanstack/react-query";
import type {
  PathfindResponse,
  PathfindBatchResponse,
  BulkConnectionResult,
  Connection,
  ConnectionCreate,
} from "@/types/connection.types";
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

// Server-side cap on items per bulk request; the UI refuses to submit past it
// rather than letting the whole batch 422 on the boundary.
export const BULK_CONNECTION_LIMIT = 200;

async function createConnectionsBulk(items: ConnectionCreate[]): Promise<BulkConnectionResult> {
  const resp = await apiClient.post<BulkConnectionResult>("/cabling/connections/bulk", { items });
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

export function useCreateConnection() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: createConnection,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["connections"] });
    },
  });
}

/**
 * Bulk create, invalidating the same ["connections"] key as useCreateConnection.
 *
 * The invalidation is unconditional on a settled request: a partially rejected
 * batch still returns 200 and still created its accepted rows, so skipping the
 * refetch when `rejected > 0` would leave the connections table stale.
 */
export function useCreateConnectionsBulk() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: createConnectionsBulk,
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

// Server-side cap on pairs per batch request (MAX_BATCH_PAIRS in the cabling
// service); pair lists beyond it are split into sequential chunks.
const PATHFIND_BATCH_LIMIT = 2000;

// One POST /cabling/pathfind/batch resolves every pair against a single
// server-side adjacency-graph build (issue #249). Results preserve request
// order, so each chunk's results zip back onto the chunk by index.
async function findPathsBatch(pairs: DevicePair[]): Promise<Map<string, PathfindResponse>> {
  const map = new Map<string, PathfindResponse>();
  for (let i = 0; i < pairs.length; i += PATHFIND_BATCH_LIMIT) {
    const chunk = pairs.slice(i, i + PATHFIND_BATCH_LIMIT);
    const resp = await apiClient.post<PathfindBatchResponse>("/cabling/pathfind/batch", {
      pairs: chunk.map((p) => ({
        source_device_id: p.sourceDeviceId,
        target_device_id: p.targetDeviceId,
      })),
    });
    chunk.forEach((pair, idx) => {
      const result = resp.data.results[idx];
      if (!result) return;
      map.set(`${pair.sourceDeviceId}::${pair.targetDeviceId}`, {
        reachable: result.reachable,
        hop_count: result.hop_count,
        paths: result.paths,
      });
    });
  }
  return map;
}

export function usePathfindPairs(pairs: DevicePair[]) {
  const key = pairs
    .map((p) => `${p.sourceDeviceId}:${p.targetDeviceId}`)
    .sort()
    .join(",");

  return useQuery({
    queryKey: ["pathfind", "pairs", key],
    queryFn: () => findPathsBatch(pairs),
    enabled: pairs.length > 0,
    staleTime: 2 * 60 * 1000,
  });
}
