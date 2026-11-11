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

function storageOrNull(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.localStorage;
  } catch {
    // Reading window.localStorage itself throws when site data is blocked.
    return null;
  }
}

/**
 * The shared tokens as storage holds them right now, or null when storage
 * cannot be read (blocked, or a getItem that throws). Null means "no
 * cross-tab view": callers fall back to this tab's own memory.
 */
export function readStoredTokens(): StoredTokens | null {
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
    // A full or blocked storage keeps this tab working from memory only.
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

interface AuthState {
  accessToken: string | null;
  refreshToken: string | null;
  user: User | null;
  isAuthenticated: boolean;
  setTokens: (accessToken: string, refreshToken: string) => void;
  setUser: (user: User | null) => void;
  clearAuth: () => void;
  /**
   * Mirror what storage holds into memory without writing storage: another
   * tab's login, refresh, or logout becomes this tab's state. A no-op when
   * storage cannot be read.
   */
  syncFromStorage: () => void;
}

const initial = readStoredTokens() ?? { accessToken: null, refreshToken: null };

export const useAuthStore = create<AuthState>((set, get) => ({
  accessToken: initial.accessToken,
  refreshToken: initial.refreshToken,
  user: null,
  isAuthenticated: !!initial.accessToken,
  setTokens: (accessToken, refreshToken) => {
    writeStoredTokens(accessToken, refreshToken);
    set({ accessToken, refreshToken, isAuthenticated: true });
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
      set({
        accessToken: stored.accessToken,
        refreshToken: stored.refreshToken,
        isAuthenticated: true,
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
 * key, or a whole-storage clear (key null), re-syncs this tab. Returns the
 * unsubscribe function; called once below, at module load.
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
