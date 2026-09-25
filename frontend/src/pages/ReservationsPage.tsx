import { useState } from "react";
import { Link } from "react-router-dom";
import { ChevronUp, ChevronDown } from "lucide-react";
import { useCancelReservation, useReleaseReservation, usePaginatedReservations } from "@/api/reservations";
import type { ReservationSort } from "@/api/reservations";
import { useAllDeviceNames } from "@/api/inventory";
import { useAuthStore } from "@/stores/authStore";
import { usePreferencesStore } from "@/stores/preferencesStore";
import type { SortState } from "@/stores/preferencesStore";
import { isAdminRole } from "@/lib/roles";
import { canCancel, canRelease } from "@/lib/reservationStatus";
import { Pagination } from "@/components/ui/Pagination";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { ReservationDetailModal } from "@/components/reservations/ReservationDetailModal";
import { CreateReservationModal } from "@/components/reservations/CreateReservationModal";
import { PurposeCategoryTag } from "@/components/reservations/PurposeCategoryTag";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { EmptyState } from "@/components/ui/EmptyState";
import type { Reservation, ReservationSortField } from "@/types/reservation.types";

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

function ReservationRow({
  reservation,
  onClick,
}: {
  reservation: Reservation;
  onClick: () => void;
}) {
  const cancel = useCancelReservation();
  const release = useReleaseReservation();
  const [confirmCancelOpen, setConfirmCancelOpen] = useState(false);
  const shortId = reservation.id.slice(0, 8);

  const start = new Date(reservation.start_time).toLocaleDateString();
  const end = new Date(reservation.end_time).toLocaleDateString();

  return (
    <tr className="border-b border-gray-100 hover:bg-gray-50 cursor-pointer" onClick={onClick}>
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
        {(canRelease(reservation.status) || canCancel(reservation.status)) && (
          <div className="flex gap-1">
            {canRelease(reservation.status) && (
              <button
                onClick={() => release.mutate(reservation.id)}
                disabled={release.isPending}
                aria-label={`Release reservation ${shortId}`}
                className="text-xs text-green-600 hover:text-green-800 px-2 py-1 rounded hover:bg-green-50 disabled:opacity-50"
              >
                Release
              </button>
            )}
            {canCancel(reservation.status) && (
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

  const { data, isLoading, isError } = usePaginatedReservations(skip, limit, allReservations, sort);
  const { data: deviceNames } = useAllDeviceNames();
  const reservations = data?.items;
  const total = data?.total ?? 0;

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
          <button
            onClick={() => setCreateOpen(true)}
            className="ml-auto px-3 py-1.5 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 transition-colors"
          >
            New Reservation
          </button>
        </div>

        <div className="bg-white rounded-lg border border-gray-200 overflow-hidden">
          {isLoading && (
            <p role="status" aria-live="polite" className="text-sm text-gray-400 text-center py-8">
              Loading reservations...
            </p>
          )}
          {isError && (
            <p className="text-sm text-red-500 text-center py-8">Failed to load reservations</p>
          )}
          {reservations && reservations.length === 0 && (
            <EmptyState>No reservations yet</EmptyState>
          )}
          {reservations && reservations.length > 0 && (
            <div className="overflow-x-auto">
            <table className="w-full min-w-[900px]">
              <thead>
                <tr className="border-b border-gray-200">
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
                    onClick={() => setSelectedReservation(res)}
                  />
                ))}
              </tbody>
            </table>
            </div>
          )}
          <Pagination total={total} skip={skip} limit={limit} onPageChange={setSkip} />
        </div>

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
      </div>
    </div>
  );
}
