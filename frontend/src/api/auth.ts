import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { LoginRequest, RegisterRequest, TokenResponse, User } from "@/types/auth.types";
import apiClient from "./client";
import { useAuthStore } from "@/stores/authStore";

async function login(data: LoginRequest): Promise<TokenResponse> {
  const resp = await apiClient.post<TokenResponse>("/auth/login", data);
  return resp.data;
}

async function register(data: RegisterRequest): Promise<User> {
  const resp = await apiClient.post<User>("/auth/register", data);
  return resp.data;
}

async function getCurrentUser(): Promise<User> {
  const resp = await apiClient.get<User>("/auth/me");
  return resp.data;
}

async function logout(refreshToken: string): Promise<void> {
  await apiClient.post("/auth/logout", { refresh_token: refreshToken });
}

export function useLogin() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: login,
    onSuccess: (data) => {
      useAuthStore.getState().setTokens(data.access_token, data.refresh_token);
      queryClient.invalidateQueries({ queryKey: ["currentUser"] });
    },
  });
}

export function useRegister() {
  return useMutation({ mutationFn: register });
}

export function useCurrentUser() {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);
  return useQuery({
    queryKey: ["currentUser"],
    queryFn: getCurrentUser,
    enabled: isAuthenticated,
    retry: false,
  });
}

export function useLogout() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => {
      const refreshToken = useAuthStore.getState().refreshToken ?? "";
      return logout(refreshToken);
    },
    onSettled: () => {
      useAuthStore.getState().clearAuth();
      queryClient.clear();
    },
  });
}
