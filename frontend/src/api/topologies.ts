import { useMutation, useQuery, useQueryClient, keepPreviousData } from "@tanstack/react-query";
import toast from "react-hot-toast";
import type {
  Topology,
  TopologyCreate,
  TopologyUpdate,
  TopologyVersion,
  TopologyVersionDetail,
  TopologyDiff,
  RestoreRequest,
} from "@/types/topology.types";
import type { PaginatedResponse } from "@/types/pagination.types";
import apiClient from "./client";

function errorDetail(err: unknown, fallback: string): string {
  const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  return typeof detail === "string" ? detail : fallback;
}

async function fetchPaginatedTopologies(skip = 0, limit = 50): Promise<PaginatedResponse<Topology>> {
  const resp = await apiClient.get<PaginatedResponse<Topology>>("/cabling/topologies", {
    params: { skip, limit },
  });
  return resp.data;
}

async function fetchTopologies(): Promise<Topology[]> {
  const resp = await fetchPaginatedTopologies(0, 500);
  return resp.items;
}

async function fetchTopology(id: string): Promise<Topology> {
  const resp = await apiClient.get<Topology>(`/cabling/topologies/${id}`);
  return resp.data;
}

async function createTopology(data: TopologyCreate): Promise<Topology> {
  const resp = await apiClient.post<Topology>("/cabling/topologies", data);
  return resp.data;
}

async function updateTopology({ id, ...data }: TopologyUpdate & { id: string }): Promise<Topology> {
  const resp = await apiClient.put<Topology>(`/cabling/topologies/${id}`, data);
  return resp.data;
}

async function deleteTopology(id: string): Promise<void> {
  await apiClient.delete(`/cabling/topologies/${id}`);
}

async function cloneTopology({ id, name }: { id: string; name: string }): Promise<Topology> {
  const resp = await apiClient.post<Topology>(`/cabling/topologies/${id}/clone`, { name });
  return resp.data;
}

export function useTopologies() {
  return useQuery({
    queryKey: ["topologies"],
    queryFn: fetchTopologies,
  });
}

export function usePaginatedTopologies(skip = 0, limit = 50) {
  return useQuery({
    queryKey: ["topologies", "paginated", skip, limit],
    queryFn: () => fetchPaginatedTopologies(skip, limit),
    placeholderData: keepPreviousData,
  });
}

export function useTopology(id: string | undefined) {
  return useQuery({
    queryKey: ["topologies", id],
    queryFn: () => fetchTopology(id!),
    enabled: !!id,
  });
}

export function useCreateTopology() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: createTopology,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["topologies"] }),
    onError: (err) => toast.error(errorDetail(err, "Failed to create topology")),
  });
}

export function useUpdateTopology() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: updateTopology,
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ["topologies"] });
      queryClient.setQueryData(["topologies", data.id], data);
    },
    onError: (err) => toast.error(errorDetail(err, "Failed to save topology")),
  });
}

export function useDeleteTopology() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: deleteTopology,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["topologies"] }),
    onError: (err) => toast.error(errorDetail(err, "Failed to delete topology")),
  });
}

export function useCloneTopology() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: cloneTopology,
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ["topologies"] });
      queryClient.setQueryData(["topologies", data.id], data);
    },
    onError: (err) => toast.error(errorDetail(err, "Failed to clone topology")),
  });
}

async function fetchVersions(
  topologyId: string,
  skip = 0,
  limit = 50,
): Promise<PaginatedResponse<TopologyVersion>> {
  const resp = await apiClient.get<PaginatedResponse<TopologyVersion>>(
    `/cabling/topologies/${topologyId}/versions`,
    { params: { skip, limit } },
  );
  return resp.data;
}

async function fetchVersion(
  topologyId: string,
  versionId: string,
): Promise<TopologyVersionDetail> {
  const resp = await apiClient.get<TopologyVersionDetail>(
    `/cabling/topologies/${topologyId}/versions/${versionId}`,
  );
  return resp.data;
}

async function fetchVersionDiff(
  topologyId: string,
  a: string,
  b: string,
): Promise<TopologyDiff> {
  const resp = await apiClient.get<TopologyDiff>(
    `/cabling/topologies/${topologyId}/versions/diff`,
    { params: { a, b } },
  );
  return resp.data;
}

async function restoreVersion(
  topologyId: string,
  versionId: string,
  body: RestoreRequest,
): Promise<Topology> {
  const resp = await apiClient.post<Topology>(
    `/cabling/topologies/${topologyId}/versions/${versionId}/restore`,
    body,
  );
  return resp.data;
}

export function useTopologyVersions(topologyId: string | undefined, skip = 0, limit = 50) {
  return useQuery({
    queryKey: ["topologies", topologyId, "versions", skip, limit],
    queryFn: () => fetchVersions(topologyId!, skip, limit),
    enabled: !!topologyId,
    placeholderData: keepPreviousData,
  });
}

export function useTopologyVersion(
  topologyId: string | undefined,
  versionId: string | undefined,
) {
  return useQuery({
    queryKey: ["topologies", topologyId, "versions", "detail", versionId],
    queryFn: () => fetchVersion(topologyId!, versionId!),
    enabled: !!topologyId && !!versionId,
  });
}

export function useVersionDiff(
  topologyId: string | undefined,
  a: string | undefined,
  b: string | undefined,
) {
  return useQuery({
    queryKey: ["topologies", topologyId, "versions", "diff", a, b],
    queryFn: () => fetchVersionDiff(topologyId!, a!, b!),
    enabled: !!topologyId && !!a && !!b && a !== b,
  });
}

export function useRestoreVersion(topologyId: string | undefined) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ versionId, body }: { versionId: string; body: RestoreRequest }) =>
      restoreVersion(topologyId!, versionId, body),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ["topologies", topologyId, "versions"] });
      queryClient.setQueryData(["topologies", data.id], data);
      queryClient.invalidateQueries({ queryKey: ["topologies"] });
    },
  });
}
