import { create } from "zustand";
import { getPreferences, patchPreferences } from "@/api/userProfile";
import type { PreferencesPatch } from "@/api/userProfile";

// A page's persisted column sort: which field, which direction. Stored under
// the generic `extras` bucket (issue #844), keyed `sort:<page>` so it sits
// alongside `page_sizes`'s per-page keying (the getPageSize/setPageSize
// precedent, issue #599) without needing its own top-level Preferences field.
export interface SortState {
  sortBy: string;
  sortDir: "asc" | "desc";
}

function sortExtraKey(page: string): string {
  return `sort:${page}`;
}

function isSortState(value: unknown): value is SortState {
  if (typeof value !== "object" || value === null) return false;
  const v = value as Record<string, unknown>;
  return (
    typeof v.sortBy === "string" && (v.sortDir === "asc" || v.sortDir === "desc")
  );
}

interface PreferencesState {
  savedFilters: Record<string, unknown>;
  pageSizes: Record<string, number>;
  extras: Record<string, unknown>;
  loaded: boolean;
  load: () => Promise<void>;
  setSavedFilter: (page: string, filter: unknown) => void;
  getPageSize: (page: string, fallback: number) => number;
  setPageSize: (page: string, size: number) => void;
  getSortState: (page: string) => SortState | null;
  setSortState: (page: string, sort: SortState | null) => void;
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

// Writes made before the preferences GET resolves (issue #985). A page builds
// its filter object from whatever the store holds, and before the load that is
// the defaults (an empty search), so sending such an object as it stands would
// overwrite the saved values with defaults the user never chose. Until `loaded`
// is true nothing is sent: each write is recorded here, and when the load
// settles the store applies only what the user changed on top of the loaded
// value and queues the merged result once.
interface PreLoadFilterWrite {
  latest: unknown;
  // Keys of an object filter the user changed. null means the filter was not
  // a plain object, so the whole value is the user's choice.
  touched: Set<string> | null;
}

let preLoadFilters: Record<string, PreLoadFilterWrite> = {};
let preLoadPageSizes: Record<string, number> = {};
let preLoadExtras: Record<string, unknown> = {};

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

// Every page filter serializes its defaults as absent or the empty string, so
// absent, null, and "" are the same unchosen value when comparing writes.
function isUnset(value: unknown): boolean {
  return value === undefined || value === null || value === "";
}

function sameFilterValue(a: unknown, b: unknown): boolean {
  if (isUnset(a) && isUnset(b)) return true;
  return JSON.stringify(a) === JSON.stringify(b);
}

function recordPreLoadFilter(page: string, filter: unknown) {
  const prior = preLoadFilters[page];
  if (!isPlainObject(filter) || (prior && prior.touched === null)) {
    preLoadFilters[page] = { latest: filter, touched: null };
    return;
  }
  const previous = prior && isPlainObject(prior.latest) ? prior.latest : {};
  const touched = new Set(prior?.touched ?? []);
  for (const key of new Set([...Object.keys(previous), ...Object.keys(filter)])) {
    if (!sameFilterValue(previous[key], filter[key])) touched.add(key);
  }
  preLoadFilters[page] = { latest: filter, touched };
}

function mergePreLoadFilter(loaded: unknown, write: PreLoadFilterWrite): unknown {
  if (write.touched === null) return write.latest;
  const latest = write.latest as Record<string, unknown>;
  const merged: Record<string, unknown> = isPlainObject(loaded) ? { ...loaded } : {};
  for (const key of write.touched) {
    if (key in latest) merged[key] = latest[key];
    else delete merged[key];
  }
  return merged;
}

function resetPreLoadWrites() {
  preLoadFilters = {};
  preLoadPageSizes = {};
  preLoadExtras = {};
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
    let savedFilters: Record<string, unknown> = {};
    let pageSizes: Record<string, number> = {};
    let extras: Record<string, unknown> = {};
    try {
      const prefs = await getPreferences();
      savedFilters = { ...(prefs.saved_filters ?? {}) };
      pageSizes = { ...(prefs.page_sizes ?? {}) };
      extras = { ...(prefs.extras ?? {}) };
    } catch {
      // If the service is unavailable or returns an error, fall back to
      // defaults; writes made while waiting still go through below.
    }
    // Apply the writes made while the load was in flight on top of what it
    // returned, then save the merged values once (issue #985).
    const filterWrites: Record<string, unknown> = {};
    for (const [page, write] of Object.entries(preLoadFilters)) {
      const merged = mergePreLoadFilter(savedFilters[page], write);
      savedFilters[page] = merged;
      filterWrites[page] = merged;
    }
    Object.assign(pageSizes, preLoadPageSizes);
    Object.assign(extras, preLoadExtras);
    const patch: PreferencesPatch = {
      saved_filters: filterWrites,
      page_sizes: { ...preLoadPageSizes },
      extras: { ...preLoadExtras },
    };
    const hasWrites =
      Object.keys(filterWrites).length > 0 ||
      Object.keys(preLoadPageSizes).length > 0 ||
      Object.keys(preLoadExtras).length > 0;
    resetPreLoadWrites();
    set({ savedFilters, pageSizes, extras, loaded: true });
    if (hasWrites) queuePatch(patch);
  },

  setSavedFilter: (page, filter) => {
    const next = { ...get().savedFilters, [page]: filter };
    set({ savedFilters: next });
    if (!get().loaded) {
      recordPreLoadFilter(page, filter);
      return;
    }
    queuePatch({ saved_filters: { [page]: filter } });
  },

  getPageSize: (page, fallback) => {
    const stored = get().pageSizes[page];
    return typeof stored === "number" ? stored : fallback;
  },

  setPageSize: (page, size) => {
    const next = { ...get().pageSizes, [page]: size };
    set({ pageSizes: next });
    if (!get().loaded) {
      preLoadPageSizes[page] = size;
      return;
    }
    queuePatch({ page_sizes: { [page]: size } });
  },

  getSortState: (page) => {
    const stored = get().extras[sortExtraKey(page)];
    return isSortState(stored) ? stored : null;
  },

  setSortState: (page, sort) => {
    const key = sortExtraKey(page);
    const next = { ...get().extras, [key]: sort };
    set({ extras: next });
    // extras merges per key server-side (no delete verb), so clearing back to
    // the default sort still sends the key, just with a null value; getSortState
    // treats a non-SortState value (including null) the same as absent.
    if (!get().loaded) {
      preLoadExtras[key] = sort;
      return;
    }
    queuePatch({ extras: { [key]: sort } });
  },

  clear: () => {
    if (patchTimer !== null) {
      clearTimeout(patchTimer);
      patchTimer = null;
    }
    pendingPatch = {};
    resetPreLoadWrites();
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
