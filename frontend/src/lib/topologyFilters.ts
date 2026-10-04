import type { SortState } from "@/stores/preferencesStore";

/**
 * Persisted list controls for the Topologies page (issue #958), read and
 * written ONLY through the functions here, like inventoryFilters.ts: a saved
 * value outside what the cabling list endpoint accepts falls back to the
 * default and is never sent, so a stale preference can never 422 the list.
 */

// The values GET /cabling/topologies accepts for `owner`, `sort_by`, and
// `sort_dir` (the Literal allowlists in services/cabling/app/routes/topologies.py).
export const TOPOLOGY_OWNER_FILTERS = ["all", "mine"] as const;
export type TopologyOwnerFilter = (typeof TOPOLOGY_OWNER_FILTERS)[number];

export const TOPOLOGY_SORT_FIELDS = ["name", "owner_name", "created_at", "updated_at"] as const;
export type TopologySortField = (typeof TOPOLOGY_SORT_FIELDS)[number];

/** The backend's own default order, shown on the Updated heading when nothing is chosen. */
export const DEFAULT_TOPOLOGY_SORT: { sortBy: TopologySortField; sortDir: "asc" | "desc" } = {
  sortBy: "updated_at",
  sortDir: "desc",
};

export const TOPOLOGY_SORT_PAGE_KEY = "topologies";

export interface TopologyFilterState {
  search: string;
  owner: TopologyOwnerFilter;
}

// The persisted `savedFilters.topologies` object. `search` is always written;
// `owner` is omitted at its default ("all"), so a cleared filter and a
// never-set one are the same object.
export interface TopologySavedFilter {
  search?: string;
  owner?: TopologyOwnerFilter;
}

export function parseSavedTopologyFilter(raw: unknown): TopologyFilterState {
  const obj = raw !== null && typeof raw === "object" ? (raw as Record<string, unknown>) : {};
  return {
    search: typeof obj.search === "string" ? obj.search : "",
    owner: obj.owner === "mine" ? "mine" : "all",
  };
}

export function serializeTopologyFilter(state: TopologyFilterState): TopologySavedFilter {
  const out: TopologySavedFilter = { search: state.search };
  if (state.owner !== "all") out.owner = state.owner;
  return out;
}

export function isTopologySortField(value: unknown): value is TopologySortField {
  return (TOPOLOGY_SORT_FIELDS as readonly unknown[]).includes(value);
}

/**
 * The explicit sort choice stored under `extras["sort:topologies"]`, or null
 * for "none chosen, use the backend default". A stored field this page does
 * not sort by (a foreign or older value in the shared bucket) is null too.
 */
export function parseTopologySort(
  raw: SortState | null,
): { sortBy: TopologySortField; sortDir: "asc" | "desc" } | null {
  if (!raw || !isTopologySortField(raw.sortBy)) return null;
  if (raw.sortDir !== "asc" && raw.sortDir !== "desc") return null;
  return { sortBy: raw.sortBy, sortDir: raw.sortDir };
}

/**
 * The next persisted sort after a click on a heading, the Reservations cycle
 * (issue #844): a new field starts ascending, a second click goes descending,
 * a third clears back to the default. "Same field" is judged against the
 * EXPLICIT choice, so the first click on Updated while the default order is
 * showing starts a fresh ascending sort instead of clearing.
 */
export function nextTopologySort(
  explicit: { sortBy: TopologySortField; sortDir: "asc" | "desc" } | null,
  field: TopologySortField,
): SortState | null {
  if (!explicit || explicit.sortBy !== field) return { sortBy: field, sortDir: "asc" };
  if (explicit.sortDir === "asc") return { sortBy: field, sortDir: "desc" };
  return null;
}
