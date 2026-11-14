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
 *    on, and the request is retried with it, with no refresh call. A stored
 *    token of ANOTHER account is never used for this tab's request: the store
 *    signs the tab out instead (`syncFromStorage`).
 * 2. Refresh is single-flight per tab (one promise every 401 of the tab
 *    awaits) and, where `navigator.locks` exists, across tabs (an exclusive
 *    Web Lock, REFRESH_LOCK_NAME). Logout takes the same lock. A tab waits for
 *    the lock at most LOCK_WAIT_TIMEOUT_MS, then goes on without it, so a
 *    frozen or hung holder cannot block the others forever. Without Web Locks
 *    (a page that is not a secure context) there is no cross-tab exclusion.
 * 3. A refresh 401 clears the shared tokens only when storage still holds the
 *    refused refresh token (or none) after REFUSAL_GRACE_MS; when storage has
 *    moved on, this tab adopts what is stored.
 * 4. A refreshed pair is written only when storage still holds the refresh
 *    token that was sent (compare and set). Otherwise another tab logged out
 *    or logged in meanwhile: that tab wins, and the new pair is revoked.
 * 5. Only a 401 from the refresh route is a refusal. A network error, a
 *    timeout, or any other status rejects the waiting requests and keeps the
 *    tokens, so an auth outage does not sign every tab out.
 */
export const REFRESH_LOCK_NAME = "herd-auth-refresh";

/**
 * A refresh refused while storage still holds the refused token polls storage
 * this long for another tab's write before it concludes the session ended,
 * with or without Web Locks: a lock grant and the propagation of another
 * tab's storage write are not ordered across processes, so the lock holder
 * can still read the old token. A genuinely revoked session therefore signs
 * out up to this much later. The race that remains: another tab's new pair
 * reaching this tab's view of storage later than this. This tab then clears
 * storage, which signs out every tab, and that tab's compare and set (rule 4)
 * finds storage empty, so it revokes its new pair instead of writing it.
 */
export const REFUSAL_GRACE_MS = 2000;
const REFUSAL_POLL_MS = 50;

/** A refresh that never answers would hold the cross-tab lock; give up after this. */
export const REFRESH_TIMEOUT_MS = 15000;

/** How long a tab waits for the refresh lock before it goes on without it. */
export const LOCK_WAIT_TIMEOUT_MS = REFRESH_TIMEOUT_MS + 5000;

/** The client-side limit on `POST /api/auth/login` (issue #1333). */
export const LOGIN_TIMEOUT_MS = 15000;

/**
 * Run `send` with an AbortSignal that fires after `ms`. axios rejects an
 * aborted request at once with a CanceledError that carries no response.
 */
export async function withClientTimeout<T>(
  ms: number,
  send: (signal: AbortSignal) => Promise<T>,
): Promise<T> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), ms);
  try {
    return await send(controller.signal);
  } finally {
    clearTimeout(timer);
  }
}

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

/**
 * Run `work` holding the cross-tab refresh lock when Web Locks exist. When the
 * lock is not granted within LOCK_WAIT_TIMEOUT_MS (or the lock manager
 * refuses), `work` runs without it.
 */
async function underRefreshLock<T>(work: () => Promise<T>): Promise<T> {
  const locks = webLocks();
  if (!locks) return work();
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), LOCK_WAIT_TIMEOUT_MS);
  let granted = false;
  try {
    return await locks
      .request(REFRESH_LOCK_NAME, { signal: controller.signal }, () => {
        granted = true;
        clearTimeout(timer);
        return work();
      })
      .then((value) => value);
  } catch (error) {
    if (!granted) return work();
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

/** Storage now, or this tab's memory when storage cannot be used. */
function currentTokens(): StoredTokens {
  const stored = readStoredTokens();
  if (stored) return stored;
  const { accessToken, refreshToken } = useAuthStore.getState();
  return { accessToken, refreshToken };
}

/**
 * Take what storage holds as this tab's state and return the access token to
 * retry with. When the store did not adopt it (another account's token: the
 * tab was signed out instead), the request fails with `failure`.
 */
function adoptStored(stored: StoredTokens, failure: unknown): string {
  useAuthStore.getState().syncFromStorage();
  const adopted = useAuthStore.getState().accessToken;
  if (stored.accessToken && adopted === stored.accessToken) return adopted;
  throw failure;
}

function revokeQuietly(refreshToken: string): void {
  axios.post("/api/auth/logout", { refresh_token: refreshToken }).catch(() => {});
}

async function waitForRefreshTokenChange(refused: string): Promise<StoredTokens | null> {
  const deadline = Date.now() + REFUSAL_GRACE_MS;
  let stored = readStoredTokens();
  while (stored && stored.refreshToken === refused && Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, REFUSAL_POLL_MS));
    stored = readStoredTokens();
  }
  return stored;
}

async function onRefreshRefused(refused: string, refusal: unknown): Promise<string> {
  const stored = await waitForRefreshTokenChange(refused);
  if (!stored || !stored.refreshToken || stored.refreshToken === refused) {
    // The refused token is still the shared one (or storage holds none, or
    // cannot be used): the session really ended, in every tab.
    useAuthStore.getState().clearAuth();
    throw refusal;
  }
  // Storage moved on (another tab refreshed or logged in): adopt it.
  return adoptStored(stored, refusal);
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
  return adoptStored(stored, new Error("Signed out in another tab"));
}

async function refreshOnce(failedAccess: string | null, original: unknown): Promise<string> {
  const tokens = currentTokens();
  if (tokens.accessToken && tokens.accessToken !== failedAccess) {
    return adoptStored(tokens, original);
  }
  const refreshToken = tokens.refreshToken;
  if (!refreshToken) {
    useAuthStore.getState().clearAuth();
    throw original;
  }
  let data: { access_token: string; refresh_token: string };
  try {
    ({ data } = await withClientTimeout(REFRESH_TIMEOUT_MS, (signal) =>
      axios.post("/api/auth/refresh", { refresh_token: refreshToken }, { signal }),
    ));
  } catch (refreshError) {
    if (axios.isAxiosError(refreshError) && refreshError.response?.status === 401) {
      return onRefreshRefused(refreshToken, refreshError);
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
    refreshInFlight = underRefreshLock(() => refreshOnce(failedAccess, original)).finally(() => {
      refreshInFlight = null;
    });
  }
  return refreshInFlight;
}

/**
 * Logout (issue #1316): waits for this tab's refresh in flight, then, under
 * the same cross-tab lock as a refresh, revokes the refresh token storage
 * holds at that moment (memory when storage cannot be used) through `send`,
 * and clears the shared tokens whether or not `send` succeeded. A refresh in
 * another tab that holds the lock therefore finishes first, and the token it
 * stored is the one revoked.
 */
export async function logoutUnderRefreshLock(
  send: (refreshToken: string) => Promise<void>,
): Promise<void> {
  if (refreshInFlight) await refreshInFlight.catch(() => undefined);
  await underRefreshLock(async () => {
    try {
      await send(currentTokens().refreshToken ?? "");
    } finally {
      useAuthStore.getState().clearAuth();
    }
  });
}

/**
 * Routes whose 401 is an answer about the request itself, never an expired
 * access token, so the interceptor below passes it through without a refresh:
 * the sign-in routes, logout, the public login-options read, and the single
 * sign-on handoff exchange (ADR 0016: a refused handoff is final, and the page
 * sending it has no session to refresh). Each entry matches the request path
 * (query string ignored) when the path ends with it, so `/auth/login` does not
 * also cover `/auth/login-options`; every entry stands on its own.
 */
export const REFRESH_SKIP_PATHS: readonly string[] = [
  "/auth/refresh",
  "/auth/login",
  "/auth/logout",
  "/auth/login-options",
  "/auth/oidc/exchange",
];

export function skipsRefresh(url: string | undefined): boolean {
  const path = (url ?? "").split(/[?#]/, 1)[0];
  return REFRESH_SKIP_PATHS.some((skipped) => path.endsWith(skipped));
}

apiClient.interceptors.response.use(
  (response) => response,
  async (error: AxiosError) => {
    const originalRequest = error.config as RetryableRequest | undefined;
    if (!originalRequest) return Promise.reject(error);

    if (
      error.response?.status !== 401 ||
      skipsRefresh(originalRequest.url) ||
      originalRequest._retry
    ) {
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
