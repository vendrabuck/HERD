import { create } from "zustand";
import type { User } from "@/types/auth.types";

/**
 * The browser keeps ONE session per profile: every open tab shares these two
 * localStorage keys (issue #1316). The access token is written LAST and
 * removed FIRST, so a tab that reads a new access token can rely on the
 * matching refresh token already being stored.
 */
export const ACCESS_TOKEN_KEY = "access_token";
export const REFRESH_TOKEN_KEY = "refresh_token";

export interface StoredTokens {
  accessToken: string | null;
  refreshToken: string | null;
}

// Set once a write to storage fails (a full quota): from then on this tab
// never reads tokens from storage again, because storage would hand back an
// older pair than the one this tab holds in memory.
let storageWriteFailed = false;

function storageOrNull(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.localStorage;
  } catch {
    // Reading window.localStorage itself throws when site data is blocked.
    return null;
  }
}

/**
 * The shared tokens as storage holds them right now, or null when this tab
 * cannot use storage: it cannot be read (blocked, or a getItem that throws),
 * or an earlier write from this tab failed. Null means "no cross-tab view":
 * callers fall back to this tab's own memory and never adopt from storage.
 */
export function readStoredTokens(): StoredTokens | null {
  if (storageWriteFailed) return null;
  const storage = storageOrNull();
  if (!storage) return null;
  try {
    return {
      accessToken: storage.getItem(ACCESS_TOKEN_KEY),
      refreshToken: storage.getItem(REFRESH_TOKEN_KEY),
    };
  } catch {
    return null;
  }
}

function writeStoredTokens(accessToken: string, refreshToken: string): void {
  const storage = storageOrNull();
  if (!storage) return;
  try {
    storage.setItem(REFRESH_TOKEN_KEY, refreshToken);
    storage.setItem(ACCESS_TOKEN_KEY, accessToken);
  } catch {
    storageWriteFailed = true;
  }
}

function removeStoredTokens(): void {
  const storage = storageOrNull();
  if (!storage) return;
  try {
    storage.removeItem(ACCESS_TOKEN_KEY);
    storage.removeItem(REFRESH_TOKEN_KEY);
  } catch {
    // Nothing more can be done; memory is cleared by the caller.
  }
}

/**
 * The `sub` claim of an access token, or null when the token is not a JWT
 * whose payload decodes to an object with a string `sub`. Read only to tell
 * one account's token from another's; never trusted for anything else (the
 * services verify the signature).
 */
export function tokenSubject(token: string | null | undefined): string | null {
  if (typeof token !== "string") return null;
  const parts = token.split(".");
  if (parts.length !== 3) return null;
  try {
    const base64 = parts[1].replace(/-/g, "+").replace(/_/g, "/");
    const padded = base64 + "=".repeat((4 - (base64.length % 4)) % 4);
    const payload: unknown = JSON.parse(atob(padded));
    const sub = (payload as { sub?: unknown } | null)?.sub;
    return typeof sub === "string" && sub.length > 0 ? sub : null;
  } catch {
    return null;
  }
}

function sameSubject(a: string, b: string): boolean {
  const left = tokenSubject(a);
  return left !== null && left === tokenSubject(b);
}

interface AuthState {
  accessToken: string | null;
  refreshToken: string | null;
  user: User | null;
  isAuthenticated: boolean;
  /**
   * The subject of the last access token this tab held. It survives a sign
   * out, so AuthGuard can bind a recorded return path to the account that was
   * signed in (issue #1333).
   */
  lastSubject: string | null;
  setTokens: (accessToken: string, refreshToken: string) => void;
  setUser: (user: User | null) => void;
  clearAuth: () => void;
  /**
   * Mirror what storage holds into memory without writing storage: another
   * tab's login, refresh, or logout becomes this tab's state. A no-op when
   * storage cannot be used. When storage holds another account's token than
   * memory does, this tab is signed out first and adopts the new token on the
   * next task, so the sign-out edge is observed and rendered.
   */
  syncFromStorage: () => void;
}

const initial = readStoredTokens() ?? { accessToken: null, refreshToken: null };

export const useAuthStore = create<AuthState>((set, get) => ({
  accessToken: initial.accessToken,
  refreshToken: initial.refreshToken,
  user: null,
  isAuthenticated: !!initial.accessToken,
  lastSubject: tokenSubject(initial.accessToken),
  setTokens: (accessToken, refreshToken) => {
    writeStoredTokens(accessToken, refreshToken);
    set({
      accessToken,
      refreshToken,
      isAuthenticated: true,
      lastSubject: tokenSubject(accessToken),
    });
  },
  setUser: (user) => set({ user }),
  clearAuth: () => {
    removeStoredTokens();
    set({
      accessToken: null,
      refreshToken: null,
      user: null,
      isAuthenticated: false,
    });
  },
  syncFromStorage: () => {
    const stored = readStoredTokens();
    if (!stored) return;
    const state = get();
    if (stored.accessToken === state.accessToken && stored.refreshToken === state.refreshToken) {
      return;
    }
    if (stored.accessToken) {
      if (state.accessToken && !sameSubject(state.accessToken, stored.accessToken)) {
        // Another account's session (a late-processed "logout, then login as
        // someone else"): emit the signed-out state first, then adopt.
        set({ accessToken: null, refreshToken: null, user: null, isAuthenticated: false });
        setTimeout(() => get().syncFromStorage(), 0);
        return;
      }
      set({
        accessToken: stored.accessToken,
        refreshToken: stored.refreshToken,
        isAuthenticated: true,
        lastSubject: tokenSubject(stored.accessToken),
      });
    } else {
      set({
        accessToken: null,
        refreshToken: stored.refreshToken,
        user: null,
        isAuthenticated: false,
      });
    }
  },
}));

/**
 * Listen for the `storage` event, which the browser fires in every OTHER tab
 * of the origin when one tab writes localStorage. A change to either token
 * key, or a whole-storage clear (key null), re-syncs this tab. The refresh
 * key's event is redundant with the access key's (every write changes the
 * access token too) and costs one no-op re-read. Returns the unsubscribe
 * function; called once below, at module load.
 */
export function startStorageSync(): () => void {
  if (typeof window === "undefined") return () => {};
  const onStorage = (event: StorageEvent) => {
    if (event.key !== null && event.key !== ACCESS_TOKEN_KEY && event.key !== REFRESH_TOKEN_KEY) {
      return;
    }
    if (event.storageArea && event.storageArea !== storageOrNull()) return;
    useAuthStore.getState().syncFromStorage();
  };
  window.addEventListener("storage", onStorage);
  return () => window.removeEventListener("storage", onStorage);
}

export const stopStorageSync = startStorageSync();
