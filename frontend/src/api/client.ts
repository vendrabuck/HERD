import axios, { type AxiosError, type InternalAxiosRequestConfig } from "axios";
import { readStoredTokens, useAuthStore, type StoredTokens } from "@/stores/authStore";

interface RetryableRequest extends InternalAxiosRequestConfig {
  _retry?: boolean;
}

const apiClient = axios.create({
  baseURL: "/api",
  headers: { "Content-Type": "application/json" },
});

// Attach JWT token from store on every request
apiClient.interceptors.request.use((config) => {
  const token = useAuthStore.getState().accessToken;
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

/**
 * Silent refresh across tabs (issue #1316). Auth rotates the refresh token on
 * every use and refuses a used one with no grace window (IAM-SESSION-2), and
 * every tab of the profile shares one stored pair, so:
 *
 * 1. The decision to refresh is made on the tokens storage holds NOW, never on
 *    this tab's memory: when storage already holds an access token other than
 *    the one that got the 401, another tab (or an earlier refresh) has moved
 *    on, and the request is retried with it, with no refresh call.
 * 2. Refresh is single-flight per tab (one promise every 401 of the tab
 *    awaits) and, where `navigator.locks` exists, across tabs (an exclusive
 *    Web Lock, REFRESH_LOCK_NAME). Without Web Locks (a page that is not a
 *    secure context) there is no cross-tab exclusion; see
 *    UNLOCKED_REFUSAL_GRACE_MS for what replaces it and its race.
 * 3. A refresh 401 clears the shared tokens only when storage still holds the
 *    refused refresh token (or none); when storage has moved on, this tab
 *    adopts what is stored.
 * 4. A refreshed pair is written only when storage still holds the refresh
 *    token that was sent (compare and set). Otherwise another tab logged out
 *    or logged in meanwhile: that tab wins, and the new pair is revoked.
 * 5. Only a 401 from the refresh route is a refusal. A network error, a
 *    timeout, or any other status rejects the waiting requests and keeps the
 *    tokens, so an auth outage does not sign every tab out.
 */
export const REFRESH_LOCK_NAME = "herd-auth-refresh";

/**
 * Without Web Locks two tabs can send the same refresh token at once; auth
 * admits exactly one. The loser that sees the refusal while storage still
 * holds the refused token polls storage for this long for the winner's write
 * before it concludes the session ended. The race that remains: a winner
 * whose new pair reaches storage later than this after the loser's refusal.
 * The loser then clears storage, which signs out every tab, and the winner's
 * compare and set (rule 4) finds storage empty, so it revokes its new pair
 * instead of writing it: the tabs end consistently signed out, never split.
 */
export const UNLOCKED_REFUSAL_GRACE_MS = 2000;
const UNLOCKED_POLL_MS = 50;

/** A refresh that never answers would hold the cross-tab lock; give up after this. */
export const REFRESH_TIMEOUT_MS = 15000;

function bearerToken(header: unknown): string | null {
  if (typeof header !== "string") return null;
  return header.startsWith("Bearer ") ? header.slice("Bearer ".length) : null;
}

function webLocks(): LockManager | null {
  try {
    const locks = typeof navigator === "undefined" ? undefined : navigator.locks;
    return locks && typeof locks.request === "function" ? locks : null;
  } catch {
    return null;
  }
}

/** Storage now, or this tab's memory when storage cannot be read. */
function currentTokens(): StoredTokens {
  const stored = readStoredTokens();
  if (stored) return stored;
  const { accessToken, refreshToken } = useAuthStore.getState();
  return { accessToken, refreshToken };
}

function revokeQuietly(refreshToken: string): void {
  axios.post("/api/auth/logout", { refresh_token: refreshToken }).catch(() => {});
}

async function waitForRefreshTokenChange(
  refused: string,
  graceMs: number,
): Promise<StoredTokens | null> {
  const deadline = Date.now() + graceMs;
  let stored = readStoredTokens();
  while (stored && stored.refreshToken === refused && Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, UNLOCKED_POLL_MS));
    stored = readStoredTokens();
  }
  return stored;
}

async function onRefreshRefused(
  refused: string,
  refusal: unknown,
  locked: boolean,
): Promise<string> {
  let stored = readStoredTokens();
  if (!locked && stored && stored.refreshToken === refused) {
    stored = await waitForRefreshTokenChange(refused, UNLOCKED_REFUSAL_GRACE_MS);
  }
  if (!stored || !stored.refreshToken || stored.refreshToken === refused) {
    // The refused token is still the shared one (or storage holds none, or
    // cannot be read): the session really ended, in every tab.
    useAuthStore.getState().clearAuth();
    throw refusal;
  }
  // Storage moved on (another tab refreshed or logged in): adopt it.
  useAuthStore.getState().syncFromStorage();
  if (stored.accessToken) return stored.accessToken;
  throw refusal;
}

function commitRefreshed(sent: string, accessToken: string, refreshToken: string): string {
  const stored = readStoredTokens();
  if (!stored || stored.refreshToken === sent) {
    useAuthStore.getState().setTokens(accessToken, refreshToken);
    return accessToken;
  }
  // Storage changed while the refresh was in flight: another tab logged out
  // (logout wins, so the new pair must not resurrect the session) or logged
  // in. Either way this pair is unused; revoke it and follow storage.
  revokeQuietly(refreshToken);
  useAuthStore.getState().syncFromStorage();
  if (stored.accessToken) return stored.accessToken;
  throw new Error("Signed out in another tab");
}

async function refreshOnce(
  failedAccess: string | null,
  original: unknown,
  locked: boolean,
): Promise<string> {
  const tokens = currentTokens();
  if (tokens.accessToken && tokens.accessToken !== failedAccess) {
    useAuthStore.getState().syncFromStorage();
    return tokens.accessToken;
  }
  const refreshToken = tokens.refreshToken;
  if (!refreshToken) {
    useAuthStore.getState().clearAuth();
    throw original;
  }
  let data: { access_token: string; refresh_token: string };
  try {
    ({ data } = await axios.post(
      "/api/auth/refresh",
      { refresh_token: refreshToken },
      { timeout: REFRESH_TIMEOUT_MS },
    ));
  } catch (refreshError) {
    if (axios.isAxiosError(refreshError) && refreshError.response?.status === 401) {
      return onRefreshRefused(refreshToken, refreshError, locked);
    }
    throw refreshError;
  }
  return commitRefreshed(refreshToken, data.access_token, data.refresh_token);
}

let refreshInFlight: Promise<string> | null = null;

/**
 * A usable access token for a request that got a 401 while carrying
 * `failedAccess`. Every 401 of this tab that arrives while a refresh is in
 * flight waits for that same refresh; each is then retried once with its
 * result, or rejected with its error.
 */
function freshAccessToken(failedAccess: string | null, original: unknown): Promise<string> {
  if (!refreshInFlight) {
    const locks = webLocks();
    // The DOM typing nests the callback's promise; at run time request()
    // settles with the callback's settled value, which then() flattens.
    const run: Promise<string> = locks
      ? locks
          .request(REFRESH_LOCK_NAME, () => refreshOnce(failedAccess, original, true))
          .then((token) => token)
      : refreshOnce(failedAccess, original, false);
    refreshInFlight = run.finally(() => {
      refreshInFlight = null;
    });
  }
  return refreshInFlight;
}

apiClient.interceptors.response.use(
  (response) => response,
  async (error: AxiosError) => {
    const originalRequest = error.config as RetryableRequest | undefined;
    if (!originalRequest) return Promise.reject(error);

    const url = originalRequest.url ?? "";
    const isAuthUrl = url.includes("/auth/refresh") || url.includes("/auth/login");

    if (error.response?.status !== 401 || isAuthUrl || originalRequest._retry) {
      return Promise.reject(error);
    }

    originalRequest._retry = true;
    const failedAccess = bearerToken(originalRequest.headers?.Authorization);
    const accessToken = await freshAccessToken(failedAccess, error);
    originalRequest.headers.Authorization = `Bearer ${accessToken}`;
    return apiClient(originalRequest);
  },
);

export default apiClient;
