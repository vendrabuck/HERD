import { useQuery, useMutation, useQueryClient, keepPreviousData } from "@tanstack/react-query";
import type { Grant, GrantCreate } from "@/types/acl.types";
import type { PaginatedResponse } from "@/types/pagination.types";
import apiClient from "./client";

interface GrantFilters {
  group_id?: string;
  resource_type?: string;
  resource_id?: string;
}

async function fetchGrants(
  filters?: GrantFilters,
  skip = 0,
  limit = 50,
): Promise<PaginatedResponse<Grant>> {
  const params: Record<string, string | number> = { skip, limit };
  if (filters?.group_id) params.group_id = filters.group_id;
  if (filters?.resource_type) params.resource_type = filters.resource_type;
  if (filters?.resource_id) params.resource_id = filters.resource_id;
  const resp = await apiClient.get<PaginatedResponse<Grant>>("/acl/grants", { params });
  return resp.data;
}

async function createGrant(data: GrantCreate): Promise<Grant> {
  const resp = await apiClient.post<Grant>("/acl/grants", data);
  return resp.data;
}

async function deleteGrant(id: string): Promise<void> {
  await apiClient.delete(`/acl/grants/${id}`);
}

// The acl service's GET /grants is paginated (items/total/skip/limit), not a
// bare array; read .data.items, not .data, when consuming this hook.
export function useGrants(filters?: GrantFilters, skip = 0, limit = 50) {
  return useQuery({
    queryKey: ["grants", filters, skip, limit],
    queryFn: () => fetchGrants(filters, skip, limit),
    placeholderData: keepPreviousData,
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
