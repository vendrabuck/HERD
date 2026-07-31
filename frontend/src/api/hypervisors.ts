import { useQuery, useMutation, useQueryClient, keepPreviousData } from "@tanstack/react-query";
import type {
  Hypervisor,
  HypervisorCreate,
  HypervisorUpdate,
} from "@/types/hypervisor.types";
import type { PaginatedResponse } from "@/types/pagination.types";
import apiClient from "./client";

async function fetchPaginatedHypervisors(
  skip = 0,
  limit = 50,
): Promise<PaginatedResponse<Hypervisor>> {
  const resp = await apiClient.get<PaginatedResponse<Hypervisor>>("/inventory/hypervisors", {
    params: { skip, limit },
  });
  return resp.data;
}

async function fetchHypervisors(): Promise<Hypervisor[]> {
  const resp = await fetchPaginatedHypervisors(0, 500);
  return resp.items;
}

async function createHypervisor(data: HypervisorCreate): Promise<Hypervisor> {
  const resp = await apiClient.post<Hypervisor>("/inventory/hypervisors", data);
  return resp.data;
}

async function updateHypervisor({
  id,
  data,
}: {
  id: string;
  data: HypervisorUpdate;
}): Promise<Hypervisor> {
  const resp = await apiClient.put<Hypervisor>(`/inventory/hypervisors/${id}`, data);
  return resp.data;
}

async function deleteHypervisor(id: string): Promise<void> {
  await apiClient.delete(`/inventory/hypervisors/${id}`);
}

export function useHypervisors() {
  return useQuery({
    queryKey: ["hypervisors"],
    queryFn: fetchHypervisors,
  });
}

export function usePaginatedHypervisors(skip = 0, limit = 50) {
  return useQuery({
    queryKey: ["hypervisors", "paginated", skip, limit],
    queryFn: () => fetchPaginatedHypervisors(skip, limit),
    placeholderData: keepPreviousData,
  });
}

export function useCreateHypervisor() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: createHypervisor,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["hypervisors"] });
    },
  });
}

export function useUpdateHypervisor() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: updateHypervisor,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["hypervisors"] });
    },
  });
}

export function useDeleteHypervisor() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: deleteHypervisor,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["hypervisors"] });
    },
  });
}
