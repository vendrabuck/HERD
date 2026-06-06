import { create } from "zustand";
import { getPreferences, patchPreferences } from "@/api/userProfile";
import type { PreferencesPatch } from "@/api/userProfile";

interface PreferencesState {
  savedFilters: Record<string, unknown>;
  pageSizes: Record<string, number>;
  extras: Record<string, unknown>;
  loaded: boolean;
  load: () => Promise<void>;
  getSavedFilter: <T = unknown>(page: string, fallback: T) => T;
  setSavedFilter: (page: string, filter: unknown) => void;
  getPageSize: (page: string, fallback: number) => number;
  setPageSize: (page: string, size: number) => void;
  clear: () => void;
}

const PATCH_DEBOUNCE_MS = 200;

let pendingPatch: PreferencesPatch = {};
let patchTimer: ReturnType<typeof setTimeout> | null = null;

function flush() {
  const payload = pendingPatch;
  pendingPatch = {};
  patchTimer = null;
  if (Object.keys(payload).length === 0) return;
  patchPreferences(payload).catch(() => {
    // Silently swallow: prefs are best-effort and shouldn't break UX.
  });
}

function scheduleFlush() {
  if (patchTimer !== null) clearTimeout(patchTimer);
  patchTimer = setTimeout(flush, PATCH_DEBOUNCE_MS);
}

function queuePatch(partial: PreferencesPatch) {
  pendingPatch = {
    saved_filters: { ...(pendingPatch.saved_filters ?? {}), ...(partial.saved_filters ?? {}) },
    page_sizes: { ...(pendingPatch.page_sizes ?? {}), ...(partial.page_sizes ?? {}) },
    extras: { ...(pendingPatch.extras ?? {}), ...(partial.extras ?? {}) },
  };
  scheduleFlush();
}

export const usePreferencesStore = create<PreferencesState>((set, get) => ({
  savedFilters: {},
  pageSizes: {},
  extras: {},
  loaded: false,

  load: async () => {
    try {
      const prefs = await getPreferences();
      set({
        savedFilters: prefs.saved_filters ?? {},
        pageSizes: prefs.page_sizes ?? {},
        extras: prefs.extras ?? {},
        loaded: true,
      });
    } catch {
      // If the service is unavailable or returns an error, fall back to defaults.
      set({ loaded: true });
    }
  },

  getSavedFilter: <T = unknown>(page: string, fallback: T): T => {
    const stored = get().savedFilters[page];
    return (stored === undefined ? fallback : (stored as T));
  },

  setSavedFilter: (page, filter) => {
    const next = { ...get().savedFilters, [page]: filter };
    set({ savedFilters: next });
    queuePatch({ saved_filters: { [page]: filter } });
  },

  getPageSize: (page, fallback) => {
    const stored = get().pageSizes[page];
    return typeof stored === "number" ? stored : fallback;
  },

  setPageSize: (page, size) => {
    const next = { ...get().pageSizes, [page]: size };
    set({ pageSizes: next });
    queuePatch({ page_sizes: { [page]: size } });
  },

  clear: () => {
    if (patchTimer !== null) {
      clearTimeout(patchTimer);
      patchTimer = null;
    }
    pendingPatch = {};
    set({ savedFilters: {}, pageSizes: {}, extras: {}, loaded: false });
  },
}));

export function _flushPendingPatchForTest() {
  if (patchTimer !== null) {
    clearTimeout(patchTimer);
    patchTimer = null;
  }
  flush();
}
