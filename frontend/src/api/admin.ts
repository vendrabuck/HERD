import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { User } from "@/types/auth.types";
import apiClient from "./client";

async function fetchAllUsers(): Promise<User[]> {
  const resp = await apiClient.get<User[]>("/auth/users");
  return resp.data;
}

async function setUserRole({ userId, role }: { userId: string; role: string }): Promise<User> {
  const resp = await apiClient.put<User>(`/auth/users/${userId}/role`, { role });
  return resp.data;
}

export function useAllUsers() {
  return useQuery({
    queryKey: ["adminUsers"],
    queryFn: fetchAllUsers,
  });
}

export function useSetUserRole() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: setUserRole,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["adminUsers"] }),
  });
}
