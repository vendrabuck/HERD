import { useEffect, useMemo, useRef, useState } from "react";
import toast from "react-hot-toast";
import { Link } from "react-router-dom";
import { ChevronUp, ChevronDown } from "lucide-react";
import {
  useBulkReservationAction,
  useCancelReservation,
  useReleaseReservation,
  usePaginatedReservations,
  usePurposeCategories,
} from "@/api/reservations";
import type { ReservationSort } from "@/api/reservations";
import { useAllDeviceNames } from "@/api/inventory";
import { useAuthStore } from "@/stores/authStore";
import { usePreferencesStore } from "@/stores/preferencesStore";
import type { SortState } from "@/stores/preferencesStore";
import { isAdminRole } from "@/lib/roles";
import { canCancelAs, canReleaseAs } from "@/lib/reservationStatus";
import { errorDetail } from "@/lib/errors";
import {
  applySettled,
  confirmDescription,
  confirmLabel,
  confirmTitle,
  keepLabel,
  noneEligibleMessage,
  partitionSelection,
  summarizeOutcome,
} from "@/lib/reservationBulk";
import type { BulkAction } from "@/lib/reservationBulk";
import {
  EMPTY_RESERVATION_FILTER,
  PERIOD_LABELS,
  PURPOSE_CATEGORY_NONE,
  RESERVATION_PERIODS,
  RESERVATION_STATUSES,
  STATUS_LABELS,
  effectivePurposeCategory,
  parseSavedReservationFilter,
  reservationListFilters,
  serializeReservationFilter,
} from "@/lib/reservationFilters";
import type { ReservationFilterState, ReservationPeriod } from "@/lib/reservationFilters";
import { purposeCategoryLabel } from "@/lib/purposeCategories";
import { Pagination } from "@/components/ui/Pagination";
import { FilterSelect, ListFilterLayout, ListFilterPanel } from "@/components/ui/ListFilterPanel";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { ReservationDetailModal } from "@/components/reservations/ReservationDetailModal";
import { CreateReservationModal } from "@/components/reservations/CreateReservationModal";
import { PurposeCategoryTag } from "@/components/reservations/PurposeCategoryTag";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { EmptyState } from "@/components/ui/EmptyState";
import type {
  Reservation,
  ReservationSortField,
  ReservationStatus,
} from "@/types/reservation.types";

// The sortable columns on this page (issue #844): each maps a visible column
// heading to one backend-allowlisted field. Period shows both start and end
// time in one column; it sorts by start_time; there is no separate heading
// for end_time so that field has no UI control here (still reachable through
// the API directly). ID, Topo ID, Topology, and Devices are not in the
// backend allowlist and stay plain headings.
const SORTABLE_COLUMN_FIELDS: ReservationSortField[] = [
  "user_id",
  "status",
  "start_time",
  "purpose_category",
];

const SORT_PAGE_KEY = "reservations";
// savedFilters key for the search and filters (issue #959).
const FILTER_PAGE_KEY = "reservations";

// Today's default ordering (created_at desc, see reservation_service.py); used
// both as the query sent when nothing is persisted and as what a third click
// clears back to. created_at has no column on this page, so it is never shown
// as "active" in a header, only applied silently.
const DEFAULT_SORT: SortState = { sortBy: "created_at", sortDir: "desc" };

function isSortableColumnField(value: string): value is ReservationSortField {
  return (SORTABLE_COLUMN_FIELDS as string[]).includes(value);
}

interface SortableHeaderProps {
  label: string;
  field: ReservationSortField;
  active: boolean;
  direction: "asc" | "desc";
  onSort: (field: ReservationSortField) => void;
}

function SortableHeader({ label, field, active, direction, onSort }: SortableHeaderProps) {
  return (
    <th
      scope="col"
      aria-sort={active ? (direction === "asc" ? "ascending" : "descending") : "none"}
      className="sticky top-0 z-10 bg-gray-50 px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase tracking-wide"
    >
      <button
        type="button"
        onClick={() => onSort(field)}
        className="flex items-center gap-1 hover:text-gray-900"
      >
        {label}
        {active &&
          (direction === "asc" ? (
            <ChevronUp className="w-3 h-3" aria-hidden="true" />
          ) : (
            <ChevronDown className="w-3 h-3" aria-hidden="true" />
          ))}
      </button>
    </th>
  );
}

const NO_SELECTION: ReadonlySet<string> = new Set();

function ReservationRow({
  reservation,
  selected,
  onToggleSelected,
  onClick,
}: {
  reservation: Reservation;
  selected: boolean;
  onToggleSelected: () => void;
  onClick: () => void;
}) {
  const cancel = useCancelReservation();
  const release = useReleaseReservation();
  const user = useAuthStore((s) => s.user);
  const mayRelease = canReleaseAs(reservation, user);
  const mayCancel = canCancelAs(reservation, user);
  const [confirmCancelOpen, setConfirmCancelOpen] = useState(false);
  const shortId = reservation.id.slice(0, 8);

  const start = new Date(reservation.start_time).toLocaleDateString();
  const end = new Date(reservation.end_time).toLocaleDateString();

  return (
    <tr
      className={`border-b border-gray-100 hover:bg-gray-50 cursor-pointer ${selected ? "bg-blue-50" : ""}`}
      onClick={onClick}
    >
      <td className="px-4 py-3 w-8" onClick={(e) => e.stopPropagation()}>
        <input
          type="checkbox"
          checked={selected}
          onChange={onToggleSelected}
          aria-label={`Select reservation ${shortId}`}
          className="h-4 w-4 rounded border-gray-300"
        />
      </td>
      <td className="px-4 py-3 text-sm font-mono text-gray-500">{shortId}</td>
      <td className="px-4 py-3 text-sm text-gray-500">{reservation.owner_name || reservation.user_id.slice(0, 8)}</td>
      <td className="px-4 py-3 text-sm">
        <StatusBadge status={reservation.status} />
      </td>
      <td className="px-4 py-3 text-sm font-mono text-gray-500 tabular-nums">
        {reservation.topology_id ? reservation.topology_id.slice(0, 8) : "-"}
      </td>
      <td className="px-4 py-3 text-sm">
        <span className="text-xs px-1.5 py-0.5 rounded bg-gray-100 text-gray-600">
          {reservation.topology_type}
        </span>
      </td>
      <td className="px-4 py-3 text-sm text-gray-600 tabular-nums">
        {reservation.device_ids.length} device{reservation.device_ids.length !== 1 ? "s" : ""}
      </td>
      <td className="px-4 py-3 text-sm text-gray-500 tabular-nums">{start} to {end}</td>
      <td className="px-4 py-3 text-sm text-gray-500">
        <div className="flex items-center gap-2">
          <span>{reservation.purpose ?? "-"}</span>
          <PurposeCategoryTag category={reservation.purpose_category} />
        </div>
      </td>
      <td className="px-4 py-3 text-sm" onClick={(e) => e.stopPropagation()}>
        {(mayRelease || mayCancel) && (
          <div className="flex gap-1">
            {mayRelease && (
              <button
                onClick={() => release.mutate(reservation.id)}
                disabled={release.isPending}
                aria-label={`Release reservation ${shortId}`}
                className="text-xs text-green-600 hover:text-green-800 px-2 py-1 rounded hover:bg-green-50 disabled:opacity-50"
              >
                Release
              </button>
            )}
            {mayCancel && (
              <button
                onClick={() => setConfirmCancelOpen(true)}
                disabled={cancel.isPending}
                aria-label={`Cancel reservation ${shortId}`}
                className="text-xs text-red-600 hover:text-red-800 px-2 py-1 rounded hover:bg-red-50 disabled:opacity-50"
              >
                Cancel
              </button>
            )}
          </div>
        )}
        <ConfirmDialog
          open={confirmCancelOpen}
          title="Cancel Reservation"
          description="Cancel this reservation? This releases its devices and cannot be undone."
          confirmLabel="Cancel reservation"
          cancelLabel="Keep reservation"
          destructive
          onConfirm={() => {
            setConfirmCancelOpen(false);
            cancel.mutate(reservation.id);
          }}
          onCancel={() => setConfirmCancelOpen(false)}
        />
      </td>
    </tr>
  );
}

export function ReservationsPage() {
  const [skip, setSkip] = useState(0);
  const [selectedReservation, setSelectedReservation] = useState<Reservation | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [showAll, setShowAll] = useState(false);
  const [confirmAction, setConfirmAction] = useState<BulkAction | null>(null);
  const bulk = useBulkReservationAction();
  const user = useAuthStore((s) => s.user);
  const isAdmin = isAdminRole(user?.role);
  const limit = 50;
  // `showAll` is admin-only; a non-admin never sets it, so the query stays
  // scoped to the caller's own reservations (issue #340).
  const allReservations = isAdmin && showAll;

  // Persisted sort choice (issue #844), keyed per page like getPageSize/
  // setPageSize (#599). A stored sortBy is validated against this page's own
  // column set before use: preferences are a generic bucket shared across
  // pages, so a stale or foreign value (an older build, a field later
  // dropped from the allowlist) falls back to the default rather than being
  // sent to the API as-is. No explicit choice means no sort params at all
  // (see fetchPaginatedReservations), so the backend's own default order
  // applies instead of this page re-stating it.
  const rawSortState = usePreferencesStore((s) => s.getSortState(SORT_PAGE_KEY));
  const setSortState = usePreferencesStore((s) => s.setSortState);
  const explicitSort: SortState | null =
    rawSortState && isSortableColumnField(rawSortState.sortBy) ? rawSortState : null;
  const sortState: SortState = explicitSort ?? DEFAULT_SORT;
  const sort: ReservationSort | undefined = explicitSort
    ? { sortBy: explicitSort.sortBy as ReservationSortField, sortDir: explicitSort.sortDir }
    : undefined;

  // Search and filters (issue #959), persisted in savedFilters.reservations and
  // read only through parseSavedReservationFilter, so a stale saved status or
  // period falls back to All. A null user value means the control is untouched
  // and the saved value shows; "" is an explicit All.
  const savedRaw = usePreferencesStore((s) => s.savedFilters[FILTER_PAGE_KEY]);
  const stored = useMemo(() => parseSavedReservationFilter(savedRaw), [savedRaw]);
  const setSavedFilter = usePreferencesStore((s) => s.setSavedFilter);
  const [userSearch, setUserSearch] = useState<string | null>(null);
  const searchInput = userSearch ?? stored.search;
  // The applied search: the saved value until the user types, then the
  // debounced typed value. The saved value applies at once (no debounce), so a
  // saved search that loads after mount is already in effect, and persisted
  // with every other field, before any other control can be changed.
  const [debouncedUserSearch, setDebouncedUserSearch] = useState<string | null>(null);
  const debouncedSearch = debouncedUserSearch ?? stored.search;
  const [userStatus, setUserStatus] = useState<ReservationStatus | "" | null>(null);
  const [userCategory, setUserCategory] = useState<string | null>(null);
  const [userPeriod, setUserPeriod] = useState<ReservationPeriod | "" | null>(null);
  const status = userStatus ?? stored.status;
  const period = userPeriod ?? stored.period;
  const rawCategory = userCategory ?? stored.purposeCategory;
  // A saved category is checked against the server's current list; the list
  // query is held while it loads so a stale category is never sent.
  const { data: categoryData, isLoading: categoriesLoading } = usePurposeCategories();
  const categories = categoryData?.categories;
  const purposeCategory = effectivePurposeCategory(rawCategory, categories);
  const categoryPending =
    rawCategory !== "" && rawCategory !== PURPOSE_CATEGORY_NONE && categoriesLoading;

  // The period anchor: one instant per view, refreshed when a filter changes,
  // never per request, so paging through one view stays consistent.
  const [periodNow, setPeriodNow] = useState(() => new Date().toISOString());

  // Every write carries the WHOLE object, so the last write holds all fields.
  const latestRef = useRef<ReservationFilterState>({
    search: debouncedSearch,
    status,
    purposeCategory,
    period,
  });
  useEffect(() => {
    latestRef.current = { search: debouncedSearch, status, purposeCategory, period };
  }, [debouncedSearch, status, purposeCategory, period]);
  const persistFilters = (patch: Partial<ReservationFilterState>) => {
    latestRef.current = { ...latestRef.current, ...patch };
    setSavedFilter(FILTER_PAGE_KEY, serializeReservationFilter(latestRef.current));
  };
  // Any filter change returns to page one under a fresh period anchor.
  const restartView = () => {
    setSkip(0);
    setPeriodNow(new Date().toISOString());
  };

  useEffect(() => {
    // Only typing arms the timer, and only when the text differs from what is
    // applied, so no stray timer can reset a page change (see InventoryPage).
    if (userSearch === null || userSearch === debouncedSearch) return;
    const timer = setTimeout(() => {
      setDebouncedUserSearch(userSearch);
      restartView();
      persistFilters({ search: userSearch });
    }, 300);
    return () => clearTimeout(timer);
    // debouncedSearch is excluded on purpose, as in InventoryPage.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [userSearch, setSavedFilter]);

  const changeStatus = (value: string) => {
    const next = RESERVATION_STATUSES.find((v) => v === value) ?? "";
    setUserStatus(next);
    restartView();
    persistFilters({ status: next });
  };
  const changeCategory = (value: string) => {
    setUserCategory(value);
    restartView();
    persistFilters({ purposeCategory: value });
  };
  const changePeriod = (value: string) => {
    const next = RESERVATION_PERIODS.find((v) => v === value) ?? "";
    setUserPeriod(next);
    restartView();
    persistFilters({ period: next });
  };
  const clearFilters = () => {
    setUserSearch("");
    setDebouncedUserSearch("");
    setUserStatus("");
    setUserCategory("");
    setUserPeriod("");
    restartView();
    persistFilters({ ...EMPTY_RESERVATION_FILTER });
  };

  const filters = reservationListFilters(
    { search: debouncedSearch, status, purposeCategory, period },
    periodNow,
  );
  const filtersApplied = Object.keys(filters).length > 0;
  const showClear = filtersApplied || searchInput !== "";

  const handleSort = (field: ReservationSortField) => {
    setSkip(0);
    if (sortState.sortBy !== field) {
      setSortState(SORT_PAGE_KEY, { sortBy: field, sortDir: "asc" });
    } else if (sortState.sortDir === "asc") {
      setSortState(SORT_PAGE_KEY, { sortBy: field, sortDir: "desc" });
    } else {
      // Third click on the same heading: clear back to the default order.
      setSortState(SORT_PAGE_KEY, null);
    }
  };

  const { data, isLoading, isError } = usePaginatedReservations(
    skip,
    limit,
    allReservations,
    sort,
    filtersApplied ? filters : undefined,
    { enabled: !categoryPending },
  );
  const { data: deviceNames } = useAllDeviceNames();
  const reservations = data?.items;
  const total = data?.total ?? 0;
  const listLoading = isLoading || (categoryPending && !data);

  // Multi-select (issue #843). The selection is tagged with the view it was
  // made in (page, sort, the all-reservations toggle, and every filter with its
  // period anchor, issue #959) and is dropped under any other view, so it can
  // never hold a row the user cannot see, even when the view changes from a
  // late-loading preference rather than a click.
  const viewKey = JSON.stringify([skip, allReservations, sort ?? null, filters]);
  const [selection, setSelection] = useState<{ key: string; ids: ReadonlySet<string> }>({
    key: viewKey,
    ids: NO_SELECTION,
  });
  // Adjust during render (not in an effect) so the stale ids are dropped for
  // good: merely reading them as empty would bring them back if the user
  // returned to the same page, sort, and toggle state.
  if (selection.key !== viewKey) setSelection({ key: viewKey, ids: NO_SELECTION });
  const selectedIds = selection.key === viewKey ? selection.ids : NO_SELECTION;
  const selectedRows = (reservations ?? []).filter((r) => selectedIds.has(r.id));
  const pageCount = reservations?.length ?? 0;
  const allSelected = pageCount > 0 && selectedRows.length === pageCount;
  const setSelectedIds = (ids: ReadonlySet<string>) => setSelection({ key: viewKey, ids });

  const toggleRow = (id: string) => {
    const next = new Set(selectedIds);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setSelectedIds(next);
  };
  const toggleAll = () =>
    setSelectedIds(allSelected ? NO_SELECTION : new Set((reservations ?? []).map((r) => r.id)));

  const partitions = {
    cancel: partitionSelection("cancel", selectedRows, user),
    release: partitionSelection("release", selectedRows, user),
  };

  const runBulk = async (action: BulkAction) => {
    setConfirmAction(null);
    const ids = partitions[action].eligible.map((r) => r.id);
    if (ids.length === 0) return;
    let results: PromiseSettledResult<unknown>[];
    try {
      results = await bulk.mutateAsync({ action, ids });
    } catch {
      results = [];
    }
    const outcome = applySettled(selectedIds, ids, results, (err) => errorDetail(err, "") || null);
    setSelectedIds(outcome.remaining);
    const message = summarizeOutcome(action, outcome);
    if (outcome.failures.length === 0) toast.success(message);
    else toast.error(message);
  };

  return (
    <div className="h-full overflow-y-auto">
      <div className="px-6 xl:px-12 2xl:px-16 py-6">
        {/* Tab toggle */}
        <div className="flex items-center gap-4 mb-4">
          <span className="text-sm px-3 py-1.5 rounded bg-gray-900 text-white">
            {allReservations ? "All Reservations" : "My Reservations"}
          </span>
          <Link
            to="/reservations/calendar"
            className="text-sm px-3 py-1.5 rounded text-gray-500 hover:text-gray-900 hover:bg-gray-100"
          >
            Calendar
          </Link>
          {data && (
            <span className="text-sm text-gray-400">({total})</span>
          )}
          <button
            onClick={() => setCreateOpen(true)}
            className="ml-auto px-3 py-1.5 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 transition-colors"
          >
            New Reservation
          </button>
        </div>

        <ListFilterLayout
          panel={
            <ListFilterPanel
              searchLabel="Search reservations"
              searchPlaceholder="Purpose or reservation ID..."
              searchValue={searchInput}
              onSearchChange={setUserSearch}
              showClear={showClear}
              onClear={clearFilters}
            >
              <FilterSelect label="Status" value={status} onChange={changeStatus}>
                <option value="">All</option>
                {RESERVATION_STATUSES.map((v) => (
                  <option key={v} value={v}>
                    {STATUS_LABELS[v]}
                  </option>
                ))}
              </FilterSelect>
              {/* "Category", not "Purpose category": the detail modal already
                  uses that text and label, and tests find it by it. */}
              <FilterSelect label="Category" value={purposeCategory} onChange={changeCategory}>
                <option value="">All</option>
                <option value={PURPOSE_CATEGORY_NONE}>Unclassified</option>
                {categories?.map((c) => (
                  <option key={c} value={c}>
                    {purposeCategoryLabel(c)}
                  </option>
                ))}
              </FilterSelect>
              <FilterSelect label="Period" value={period} onChange={changePeriod}>
                <option value="">All</option>
                {RESERVATION_PERIODS.map((v) => (
                  <option key={v} value={v}>
                    {PERIOD_LABELS[v]}
                  </option>
                ))}
              </FilterSelect>
              {isAdmin && (
                <label className="flex items-center gap-1.5 text-sm text-gray-600 cursor-pointer select-none">
                  <input
                    type="checkbox"
                    checked={showAll}
                    onChange={(e) => {
                      setShowAll(e.target.checked);
                      setSkip(0);
                    }}
                    className="h-4 w-4 rounded border-gray-300"
                  />
                  All reservations
                </label>
              )}
            </ListFilterPanel>
          }
        >
        <div role="status" aria-label="Selection" aria-live="polite">
          {selectedRows.length > 0 && (
            <div className="mb-3 flex flex-wrap items-center gap-3 rounded-lg border border-blue-200 bg-blue-50 px-4 py-2 text-sm">
              <span className="font-medium text-gray-900">{selectedRows.length} selected</span>
              {(["cancel", "release"] as const).map((action) => {
                const none = partitions[action].eligible.length === 0;
                const noteId = `bulk-${action}-note`;
                return (
                  <span key={action} className="flex items-center gap-2">
                    <button
                      type="button"
                      onClick={() => setConfirmAction(action)}
                      disabled={bulk.isPending || none}
                      aria-describedby={none ? noteId : undefined}
                      className={`text-xs px-2.5 py-1.5 rounded border disabled:opacity-50 ${
                        action === "cancel"
                          ? "border-red-300 text-red-700 hover:bg-red-50"
                          : "border-green-300 text-green-700 hover:bg-green-50"
                      }`}
                    >
                      {action === "cancel" ? "Cancel selected" : "Release selected"}
                    </button>
                    {none && (
                      <span id={noteId} className="text-xs text-gray-500">
                        {noneEligibleMessage(action, partitions[action].skipped)}
                      </span>
                    )}
                  </span>
                );
              })}
              <button
                type="button"
                onClick={() => setSelectedIds(NO_SELECTION)}
                disabled={bulk.isPending}
                className="ml-auto text-xs text-gray-600 hover:text-gray-900 disabled:opacity-50"
              >
                Clear selection
              </button>
            </div>
          )}
        </div>

        <div className="bg-white rounded-lg border border-gray-200 overflow-hidden">
          {listLoading && (
            <p role="status" aria-live="polite" className="text-sm text-gray-400 text-center py-8">
              Loading reservations...
            </p>
          )}
          {isError && (
            <p className="text-sm text-red-500 text-center py-8">Failed to load reservations</p>
          )}
          {reservations && reservations.length === 0 && (
            filtersApplied ? (
              <EmptyState>
                No reservations match the current filters.{" "}
                <button
                  type="button"
                  onClick={clearFilters}
                  className="text-blue-600 hover:underline"
                >
                  Clear filters
                </button>
              </EmptyState>
            ) : (
              <EmptyState>No reservations yet</EmptyState>
            )
          )}
          {reservations && reservations.length > 0 && (
            <div className="overflow-x-auto">
            <table className="w-full min-w-[940px]">
              <thead>
                <tr className="border-b border-gray-200">
                  <th scope="col" className="sticky top-0 z-10 bg-gray-50 px-4 py-2 w-8">
                    <input
                      type="checkbox"
                      checked={allSelected}
                      ref={(el) => {
                        if (el) el.indeterminate = selectedRows.length > 0 && !allSelected;
                      }}
                      onChange={toggleAll}
                      aria-label="Select all reservations on this page"
                      className="h-4 w-4 rounded border-gray-300"
                    />
                  </th>
                  <th className="sticky top-0 z-10 bg-gray-50 px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase tracking-wide">ID</th>
                  <SortableHeader
                    label="Owner"
                    field="user_id"
                    active={sortState.sortBy === "user_id"}
                    direction={sortState.sortDir}
                    onSort={handleSort}
                  />
                  <SortableHeader
                    label="Status"
                    field="status"
                    active={sortState.sortBy === "status"}
                    direction={sortState.sortDir}
                    onSort={handleSort}
                  />
                  <th className="sticky top-0 z-10 bg-gray-50 px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase tracking-wide">Topo ID</th>
                  <th className="sticky top-0 z-10 bg-gray-50 px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase tracking-wide">Topology</th>
                  <th className="sticky top-0 z-10 bg-gray-50 px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase tracking-wide">Devices</th>
                  <SortableHeader
                    label="Period"
                    field="start_time"
                    active={sortState.sortBy === "start_time"}
                    direction={sortState.sortDir}
                    onSort={handleSort}
                  />
                  <SortableHeader
                    label="Purpose"
                    field="purpose_category"
                    active={sortState.sortBy === "purpose_category"}
                    direction={sortState.sortDir}
                    onSort={handleSort}
                  />
                  <th className="sticky top-0 z-10 bg-gray-50 px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase tracking-wide"></th>
                </tr>
              </thead>
              <tbody>
                {reservations.map((res) => (
                  <ReservationRow
                    key={res.id}
                    reservation={res}
                    selected={selectedIds.has(res.id)}
                    onToggleSelected={() => toggleRow(res.id)}
                    onClick={() => setSelectedReservation(res)}
                  />
                ))}
              </tbody>
            </table>
            </div>
          )}
          <Pagination total={total} skip={skip} limit={limit} onPageChange={setSkip} />
        </div>
        </ListFilterLayout>

        <ReservationDetailModal
          reservation={selectedReservation}
          deviceNames={deviceNames ?? new Map()}
          onClose={() => setSelectedReservation(null)}
        />

        <CreateReservationModal
          open={createOpen}
          deviceIds={[]}
          onClose={() => setCreateOpen(false)}
        />

        <ConfirmDialog
          open={confirmAction !== null}
          title={confirmAction ? confirmTitle(confirmAction) : ""}
          description={confirmAction ? confirmDescription(confirmAction, partitions[confirmAction]) : ""}
          confirmLabel={confirmAction ? confirmLabel(confirmAction, partitions[confirmAction]) : "Confirm"}
          cancelLabel={confirmAction ? keepLabel(confirmAction) : "Cancel"}
          destructive={confirmAction === "cancel"}
          onConfirm={() => confirmAction && runBulk(confirmAction)}
          onCancel={() => setConfirmAction(null)}
        />
      </div>
    </div>
  );
}
