import { useMutation, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import type {
  LoginOptions,
  LoginRequest,
  ProfileUpdate,
  RegisterRequest,
  TokenResponse,
  User,
} from "@/types/auth.types";
import apiClient, { LOGIN_TIMEOUT_MS, logoutUnderRefreshLock, withClientTimeout } from "./client";
import { useAuthStore } from "@/stores/authStore";
import { parseLoginOptions, parseOidcExchange, type OidcExchangeResult } from "@/lib/sso";

/** Public, every mode (ADR 0016); on the refresh interceptor's skip list. */
export const LOGIN_OPTIONS_PATH = "/auth/login-options";
/** The single sign-on handoff exchange (ADR 0016 D3); on the refresh interceptor's skip list. */
export const OIDC_EXCHANGE_PATH = "/auth/oidc/exchange";

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

/**
 * The ONE way a sign-in becomes this tab's session, for a password login and
 * a single sign-on exchange alike: the pair goes through `setTokens` (shared
 * storage, so every tab follows, IAM-UI-11) and the signed-in user query is
 * refetched.
 */
export function adoptSignInTokens(
  queryClient: QueryClient,
  tokens: { access_token: string; refresh_token: string },
): void {
  useAuthStore.getState().setTokens(tokens.access_token, tokens.refresh_token);
  queryClient.invalidateQueries({ queryKey: ["currentUser"] });
}

export function useLogin() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: login,
    onSuccess: (data) => adoptSignInTokens(queryClient, data),
  });
}

/**
 * `GET /api/auth/login-options`, validated by parseLoginOptions; a body that
 * is not a valid answer rejects like a failed request, so the page falls back
 * to today's form (ADR 0016 D10).
 */
export async function getLoginOptions(): Promise<LoginOptions> {
  const resp = await apiClient.get<unknown>(LOGIN_OPTIONS_PATH);
  const options = parseLoginOptions(resp.data);
  if (!options) throw new Error("Unusable login options answer");
  return options;
}

/**
 * The active sign-in method for the login page. Not retried and not refetched
 * on focus: the page renders today's form until an answer arrives and on any
 * error, so a retry would only delay nothing.
 */
export function useLoginOptions() {
  return useQuery({
    queryKey: ["loginOptions"],
    queryFn: getLoginOptions,
    retry: false,
    refetchOnWindowFocus: false,
  });
}

/**
 * `POST /api/auth/oidc/exchange` with the body `{}` (ADR 0016 D3). The browser
 * attaches the HttpOnly handoff cookie (same origin; `withCredentials` states
 * it). Rejects on any failure, an answer without a usable token pair included;
 * resolves with the tokens and a safe in-app `next`. Nothing here logs.
 */
export async function exchangeOidcHandoff(): Promise<OidcExchangeResult> {
  const resp = await withClientTimeout(LOGIN_TIMEOUT_MS, (signal) =>
    apiClient.post<unknown>(OIDC_EXCHANGE_PATH, {}, { signal, withCredentials: true }),
  );
  const result = parseOidcExchange(resp.data);
  if (!result) throw new Error("Unusable single sign-on exchange answer");
  return result;
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
