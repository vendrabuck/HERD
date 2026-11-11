import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type {
  LoginRequest,
  ProfileUpdate,
  RegisterRequest,
  TokenResponse,
  User,
} from "@/types/auth.types";
import apiClient, { LOGIN_TIMEOUT_MS, logoutUnderRefreshLock, withClientTimeout } from "./client";
import { useAuthStore } from "@/stores/authStore";

async function login(data: LoginRequest): Promise<TokenResponse> {
  const resp = await withClientTimeout(LOGIN_TIMEOUT_MS, (signal) =>
    apiClient.post<TokenResponse>("/auth/login", data, { signal }),
  );
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

async function updateMyProfile(profile: ProfileUpdate): Promise<User> {
  const resp = await apiClient.patch<User>("/auth/me/profile", profile);
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
    mutationFn: () => logoutUnderRefreshLock(logout),
    onSettled: () => {
      useAuthStore.getState().clearAuth();
      queryClient.clear();
    },
  });
}

/**
 * Save the caller's own profile (issue #1252). On success the cached current
 * user is replaced with the server's answer, so the header and every reader of
 * useCurrentUser show the new name at once; the admin user list is refetched.
 */
export function useUpdateMyProfile() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: updateMyProfile,
    onSuccess: (user) => {
      queryClient.setQueryData(["currentUser"], user);
      queryClient.invalidateQueries({ queryKey: ["adminUsers"] });
    },
  });
}
