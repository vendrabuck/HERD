import { useQuery, useMutation, useQueryClient, keepPreviousData } from "@tanstack/react-query";
import type { Device, DeviceCreate, DeviceUpdate, DeviceFilters } from "@/types/device.types";
import type { PaginatedResponse } from "@/types/pagination.types";
import apiClient from "./client";

// Query-key factory. Splits the "devices" namespace into list queries (paginated,
// all-names, simple filtered) and detail queries (per-id). Mutations invalidate
// lists as a group while patching the detail cache surgically.
const deviceKeys = {
  all: ["devices"] as const,
  lists: () => [...deviceKeys.all, "list"] as const,
  paginated: (filters: DeviceFilters | undefined, skip: number, limit: number) =>
    [...deviceKeys.lists(), "paginated", filters, skip, limit] as const,
  simpleList: (filters: DeviceFilters | undefined) =>
    [...deviceKeys.lists(), "simple", filters] as const,
  allNames: () => [...deviceKeys.lists(), "all-names"] as const,
  details: () => [...deviceKeys.all, "detail"] as const,
  detail: (id: string) => [...deviceKeys.details(), id] as const,
};

async function fetchPaginatedDevices(
  filters?: DeviceFilters,
  skip = 0,
  limit = 50,
): Promise<PaginatedResponse<Device>> {
  const params: Record<string, string | number> = { skip, limit };
  if (filters?.template_id) params.template_id = filters.template_id;
  if (filters?.topology_type) params.topology_type = filters.topology_type;
  if (filters?.status) params.status = filters.status;
  if (filters?.dut_only) params.dut_only = "true";
  if (filters?.search) params.search = filters.search;
  const resp = await apiClient.get<PaginatedResponse<Device>>("/inventory/devices", { params });
  return resp.data;
}

async function fetchDevices(filters?: DeviceFilters): Promise<Device[]> {
  const resp = await fetchPaginatedDevices(filters, 0, 500);
  return resp.items;
}

export async function fetchDevice(id: string): Promise<Device> {
  const resp = await apiClient.get<Device>(`/inventory/devices/${id}`);
  return resp.data;
}

async function updateDevice({ id, data }: { id: string; data: DeviceUpdate }): Promise<Device> {
  const resp = await apiClient.put<Device>(`/inventory/devices/${id}`, data);
  return resp.data;
}

async function createDevice(data: DeviceCreate): Promise<Device> {
  const resp = await apiClient.post<Device>("/inventory/devices", data);
  return resp.data;
}

// Walk every page of /inventory/devices and return an id to name map.
// Exit conditions (in order): short page from the server, or skip meeting/exceeding
// the reported total. A MAX_PAGES cap prevents an infinite loop if the server
// ever returns overlapping pages (e.g., from a mid-fetch write).
async function fetchAllDeviceNames(): Promise<Map<string, string>> {
  const map = new Map<string, string>();
  const limit = 500;
  const MAX_PAGES = 200;
  let skip = 0;
  for (let page = 0; page < MAX_PAGES; page++) {
    const resp = await fetchPaginatedDevices(undefined, skip, limit);
    for (const d of resp.items) map.set(d.id, d.name);
    if (resp.items.length === 0) break;
    skip += resp.items.length;
    if (resp.items.length < limit) break;
    if (skip >= resp.total) break;
  }
  return map;
}

export function useAllDeviceNames() {
  return useQuery({
    queryKey: deviceKeys.allNames(),
    queryFn: fetchAllDeviceNames,
    staleTime: 5 * 60 * 1000,
  });
}

export function useDevice(id: string | undefined) {
  return useQuery({
    queryKey: id ? deviceKeys.detail(id) : deviceKeys.details(),
    queryFn: () => fetchDevice(id!),
    enabled: !!id,
  });
}

export function useUpdateDevice() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: updateDevice,
    onSuccess: (updated) => {
      // Patch the detail cache with the server's response rather than invalidating
      // it, so open detail views update instantly and skip a network round-trip.
      queryClient.setQueryData(deviceKeys.detail(updated.id), updated);
      // Lists (paginated, simple, all-names) may reflect name/status changes.
      queryClient.invalidateQueries({ queryKey: deviceKeys.lists() });
    },
  });
}

export function useDevices(filters?: DeviceFilters) {
  return useQuery({
    queryKey: deviceKeys.simpleList(filters),
    queryFn: () => fetchDevices(filters),
  });
}

export function usePaginatedDevices(filters?: DeviceFilters, skip = 0, limit = 50) {
  return useQuery({
    queryKey: deviceKeys.paginated(filters, skip, limit),
    queryFn: () => fetchPaginatedDevices(filters, skip, limit),
    placeholderData: keepPreviousData,
  });
}

export function useDeleteDevice() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (id: string) => {
      await apiClient.delete(`/inventory/devices/${id}`);
      return id;
    },
    onSuccess: (deletedId) => {
      queryClient.removeQueries({ queryKey: deviceKeys.detail(deletedId) });
      queryClient.invalidateQueries({ queryKey: deviceKeys.lists() });
    },
  });
}

export function useCreateDevice() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: createDevice,
    onSuccess: (created) => {
      // Seed the detail cache so an immediate navigate-to-detail is instant.
      queryClient.setQueryData(deviceKeys.detail(created.id), created);
      queryClient.invalidateQueries({ queryKey: deviceKeys.lists() });
    },
  });
}
