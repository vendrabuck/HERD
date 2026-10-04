import type { ReservationStatus } from "@/types/reservation.types";

// Values GET /reservations/ accepts for `status` (the backend ReservationStatus enum).
// A saved value outside this set is dropped on read, so a stale preference can never
// reach the API and 422.
export const RESERVATION_STATUSES: readonly ReservationStatus[] = [
  "PENDING",
  "PENDING_PROVISION",
  "ACTIVE",
  "COMPLETED",
  "CANCELLED",
  "FAILED",
];

// Option labels for the Status filter. Sentence case, not the badge's enum text,
// so a select option never duplicates a row's status badge text.
export const STATUS_LABELS: Record<ReservationStatus, string> = {
  PENDING: "Pending",
  PENDING_PROVISION: "Pending provision",
  ACTIVE: "Active",
  COMPLETED: "Completed",
  CANCELLED: "Cancelled",
  FAILED: "Failed",
};

// The backend's purpose_category value for "no category set" (issue #959).
export const PURPOSE_CATEGORY_NONE = "none";

export type ReservationPeriod = "upcoming" | "current" | "past";
export const RESERVATION_PERIODS: readonly ReservationPeriod[] = ["upcoming", "current", "past"];
export const PERIOD_LABELS: Record<ReservationPeriod, string> = {
  upcoming: "Upcoming",
  current: "Current",
  past: "Past",
};

export interface ReservationFilterState {
  search: string;
  status: ReservationStatus | "";
  // A configured category, PURPOSE_CATEGORY_NONE, or "" for All. A configured
  // category is checked against the server's list by effectivePurposeCategory.
  purposeCategory: string;
  period: ReservationPeriod | "";
}

export const EMPTY_RESERVATION_FILTER: ReservationFilterState = {
  search: "",
  status: "",
  purposeCategory: "",
  period: "",
};

// The persisted `savedFilters.reservations` object. A filter at "All" is omitted.
export interface ReservationSavedFilter {
  search?: string;
  status?: string;
  purpose_category?: string;
  period?: string;
}

export function parseSavedReservationFilter(raw: unknown): ReservationFilterState {
  const obj = raw !== null && typeof raw === "object" ? (raw as Record<string, unknown>) : {};
  return {
    search: typeof obj.search === "string" ? obj.search : "",
    status: RESERVATION_STATUSES.find((s) => s === obj.status) ?? "",
    purposeCategory: typeof obj.purpose_category === "string" ? obj.purpose_category : "",
    period: RESERVATION_PERIODS.find((p) => p === obj.period) ?? "",
  };
}

export function serializeReservationFilter(
  state: ReservationFilterState,
): ReservationSavedFilter {
  const out: ReservationSavedFilter = { search: state.search };
  if (state.status) out.status = state.status;
  if (state.purposeCategory) out.purpose_category = state.purposeCategory;
  if (state.period) out.period = state.period;
  return out;
}

/**
 * The purpose category to send: "none" always, a category only when the server's
 * current list has it, otherwise "" (All). `categories` undefined means the list
 * has not loaded; the page holds its query until it does, so a stale saved
 * category is never sent.
 */
export function effectivePurposeCategory(
  value: string,
  categories: readonly string[] | undefined,
): string {
  if (value === PURPOSE_CATEGORY_NONE) return value;
  return value && categories?.includes(value) ? value : "";
}

// Query parameters for GET /reservations/ (issue #959). `status` is an array
// because the backend takes it as a repeated parameter.
export interface ReservationListFilters {
  search?: string;
  status?: ReservationStatus[];
  purpose_category?: string;
  starts_after?: string;
  starts_before?: string;
  ends_after?: string;
  ends_before?: string;
}

/**
 * Map the panel state to list parameters. `now` is the period anchor: the page
 * fixes it when a filter changes, not per request, so every page of one view is
 * cut at the same instant. The backend bounds are half-open (an *_after bound is
 * inclusive, a *_before bound exclusive), so the three periods partition the rows:
 * upcoming starts at or after now, current started before now and ends at or after
 * it, past ended before now. Periods are by time only; status is its own filter.
 */
export function reservationListFilters(
  state: ReservationFilterState,
  now: string,
): ReservationListFilters {
  const out: ReservationListFilters = {};
  const search = state.search.trim();
  if (search) out.search = search;
  if (state.status) out.status = [state.status];
  if (state.purposeCategory) out.purpose_category = state.purposeCategory;
  if (state.period === "upcoming") out.starts_after = now;
  if (state.period === "current") {
    out.starts_before = now;
    out.ends_after = now;
  }
  if (state.period === "past") out.ends_before = now;
  return out;
}

/** Serialize list parameters with `status` repeated (status=A&status=B). */
export function appendReservationListFilters(
  params: URLSearchParams,
  filters: ReservationListFilters,
): void {
  if (filters.search) params.set("search", filters.search);
  for (const s of filters.status ?? []) params.append("status", s);
  if (filters.purpose_category) params.set("purpose_category", filters.purpose_category);
  if (filters.starts_after) params.set("starts_after", filters.starts_after);
  if (filters.starts_before) params.set("starts_before", filters.starts_before);
  if (filters.ends_after) params.set("ends_after", filters.ends_after);
  if (filters.ends_before) params.set("ends_before", filters.ends_before);
}
