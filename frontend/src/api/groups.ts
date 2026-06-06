import { useQuery, useMutation, useQueryClient, keepPreviousData } from "@tanstack/react-query";
import type { Group, GroupDetail, GroupCreate, GroupUpdate, GroupMember } from "@/types/group.types";
import type { PaginatedResponse } from "@/types/pagination.types";
import apiClient from "./client";

async function fetchPaginatedGroups(skip = 0, limit = 50): Promise<PaginatedResponse<Group>> {
  const resp = await apiClient.get<PaginatedResponse<Group>>("/auth/groups", {
    params: { skip, limit },
  });
  return resp.data;
}

async function fetchGroups(): Promise<Group[]> {
  const resp = await fetchPaginatedGroups(0, 500);
  return resp.items;
}

async function fetchGroup(id: string): Promise<GroupDetail> {
  const resp = await apiClient.get<GroupDetail>(`/auth/groups/${id}`);
  return resp.data;
}

async function createGroup(data: GroupCreate): Promise<Group> {
  const resp = await apiClient.post<Group>("/auth/groups", data);
  return resp.data;
}

async function updateGroup({ id, data }: { id: string; data: GroupUpdate }): Promise<Group> {
  const resp = await apiClient.put<Group>(`/auth/groups/${id}`, data);
  return resp.data;
}

async function deleteGroup(id: string): Promise<void> {
  await apiClient.delete(`/auth/groups/${id}`);
}

async function addMember({ groupId, userId }: { groupId: string; userId: string }): Promise<GroupMember> {
  const resp = await apiClient.post<GroupMember>(`/auth/groups/${groupId}/members`, { user_id: userId });
  return resp.data;
}

async function removeMember({ groupId, userId }: { groupId: string; userId: string }): Promise<void> {
  await apiClient.delete(`/auth/groups/${groupId}/members/${userId}`);
}

async function bulkAddMembers({
  groupId,
  userIds,
}: {
  groupId: string;
  userIds: string[];
}): Promise<{ added: number; skipped: number }> {
  const resp = await apiClient.post<{ added: number; skipped: number }>(
    `/auth/groups/${groupId}/members/bulk`,
    { user_ids: userIds }
  );
  return resp.data;
}

async function bulkRemoveMembers({
  groupId,
  userIds,
}: {
  groupId: string;
  userIds: string[];
}): Promise<{ removed: number; not_found: number }> {
  const resp = await apiClient.post<{ removed: number; not_found: number }>(
    `/auth/groups/${groupId}/members/bulk-remove`,
    { user_ids: userIds }
  );
  return resp.data;
}

export function useGroups() {
  return useQuery({
    queryKey: ["groups"],
    queryFn: fetchGroups,
  });
}

export function usePaginatedGroups(skip = 0, limit = 50) {
  return useQuery({
    queryKey: ["groups", "paginated", skip, limit],
    queryFn: () => fetchPaginatedGroups(skip, limit),
    placeholderData: keepPreviousData,
  });
}

export function useGroup(id: string | undefined) {
  return useQuery({
    queryKey: ["groups", id],
    queryFn: () => fetchGroup(id!),
    enabled: !!id,
  });
}

export function useCreateGroup() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: createGroup,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["groups"] }),
  });
}

export function useUpdateGroup() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: updateGroup,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["groups"] }),
  });
}

export function useDeleteGroup() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: deleteGroup,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["groups"] }),
  });
}

export function useAddMember() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: addMember,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["groups"] }),
  });
}

export function useRemoveMember() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: removeMember,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["groups"] }),
  });
}

export function useBulkAddMembers() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: bulkAddMembers,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["groups"] }),
  });
}

export function useBulkRemoveMembers() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: bulkRemoveMembers,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["groups"] }),
  });
}
