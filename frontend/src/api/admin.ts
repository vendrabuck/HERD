import { useMutation, useQuery, useQueryClient, keepPreviousData } from "@tanstack/react-query";
import type { ProfileUpdate, User } from "@/types/auth.types";
import type { PaginatedResponse } from "@/types/pagination.types";
import apiClient from "./client";

async function fetchPaginatedUsers(skip = 0, limit = 50): Promise<PaginatedResponse<User>> {
  const resp = await apiClient.get<PaginatedResponse<User>>("/auth/users", {
    params: { skip, limit },
  });
  return resp.data;
}

async function fetchAllUsers(): Promise<User[]> {
  const resp = await fetchPaginatedUsers(0, 500);
  return resp.items;
}

async function setUserRole({ userId, role }: { userId: string; role: string }): Promise<User> {
  const resp = await apiClient.put<User>(`/auth/users/${userId}/role`, { role });
  return resp.data;
}

async function updateUserProfile({
  userId,
  profile,
}: {
  userId: string;
  profile: ProfileUpdate;
}): Promise<User> {
  const resp = await apiClient.patch<User>(`/auth/users/${userId}/profile`, profile);
  return resp.data;
}

export function useAllUsers(enabled: boolean = true) {
  return useQuery({
    queryKey: ["adminUsers"],
    queryFn: fetchAllUsers,
    enabled,
  });
}

export function usePaginatedUsers(skip = 0, limit = 50) {
  return useQuery({
    queryKey: ["adminUsers", "paginated", skip, limit],
    queryFn: () => fetchPaginatedUsers(skip, limit),
    placeholderData: keepPreviousData,
  });
}

export function useSetUserRole() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: setUserRole,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["adminUsers"] }),
  });
}

/**
 * Save any account's profile from the Users page (issue #1252; admin or
 * superadmin). Refetches the user list, and when the edited account is the
 * caller's own, replaces the cached current user as the self edit does.
 */
export function useUpdateUserProfile() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: updateUserProfile,
    onSuccess: (user) => {
      const current = queryClient.getQueryData<User>(["currentUser"]);
      if (current && current.id === user.id) {
        queryClient.setQueryData(["currentUser"], user);
      }
      queryClient.invalidateQueries({ queryKey: ["adminUsers"] });
    },
  });
}
