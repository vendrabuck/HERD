import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import type { Grant, GrantCreate } from "@/types/acl.types";
import apiClient from "./client";

interface GrantFilters {
  group_id?: string;
  resource_type?: string;
  resource_id?: string;
}

async function fetchGrants(filters?: GrantFilters): Promise<Grant[]> {
  const params = new URLSearchParams();
  if (filters?.group_id) params.set("group_id", filters.group_id);
  if (filters?.resource_type) params.set("resource_type", filters.resource_type);
  if (filters?.resource_id) params.set("resource_id", filters.resource_id);
  const query = params.toString();
  const url = `/acl/grants${query ? `?${query}` : ""}`;
  const resp = await apiClient.get<Grant[]>(url);
  return resp.data;
}

async function createGrant(data: GrantCreate): Promise<Grant> {
  const resp = await apiClient.post<Grant>("/acl/grants", data);
  return resp.data;
}

async function deleteGrant(id: string): Promise<void> {
  await apiClient.delete(`/acl/grants/${id}`);
}

export function useGrants(filters?: GrantFilters) {
  return useQuery({
    queryKey: ["grants", filters],
    queryFn: () => fetchGrants(filters),
  });
}

export function useCreateGrant() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: createGrant,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["grants"] }),
  });
}

export function useDeleteGrant() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: deleteGrant,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["grants"] }),
  });
}
