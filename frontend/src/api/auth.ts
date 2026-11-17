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
/**
 * The client-side limit on `GET /api/auth/login-options` (issue #1420). The
 * route reads no database (IAM-OIDC-16), so an answer slower than this means
 * auth is in trouble; the page then shows D10's fallback deliberately instead
 * of its pending state forever. Kept well under the e2e helpers' 15 s wait
 * for the form, so a hung request still lets them find the fallback.
 */
export const LOGIN_OPTIONS_TIMEOUT_MS = 5000;
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
 * to today's form (ADR 0016 D10). A request that has not answered within
 * LOGIN_OPTIONS_TIMEOUT_MS is aborted and rejects the same way (issue #1420).
 */
export async function getLoginOptions(): Promise<LoginOptions> {
  const resp = await withClientTimeout(LOGIN_OPTIONS_TIMEOUT_MS, (signal) =>
    apiClient.get<unknown>(LOGIN_OPTIONS_PATH, { signal }),
  );
  const options = parseLoginOptions(resp.data);
  if (!options) throw new Error("Unusable login options answer");
  return options;
}

/**
 * The active sign-in method for the login page. The page shows no sign-in
 * method until this settles (issue #1420), so it settles as fast as it can:
 * not retried (a failure is D10's fallback at once), and sent even while the
 * browser reports itself offline (`networkMode: "always"`), because a paused
 * query would keep the page pending with no request and so no timeout to end
 * it. Never refetched in place, on window focus or on a network reconnect: a
 * refetch of a query with no answer puts it back to pending, which would take
 * D10's fallback form away from under a user filling it in. (TanStack Query
 * already defaults `refetchOnReconnect` to false under `networkMode:
 * "always"`; it is stated here so dropping the network mode cannot turn it on.)
 */
export function useLoginOptions() {
  return useQuery({
    queryKey: ["loginOptions"],
    queryFn: getLoginOptions,
    retry: false,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    networkMode: "always",
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
