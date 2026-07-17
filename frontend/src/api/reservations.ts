import { useMutation, useQuery, useQueryClient, keepPreviousData } from "@tanstack/react-query";
import toast from "react-hot-toast";
import type {
  CalendarQueryParams,
  ForkCanvasDraftResult,
  ForkConflictDetail,
  ForkSaveResult,
  Reservation,
  ReservationCreate,
  ReservationFork,
  ReservationUpdate,
  WiringRetryResponse,
  WiringStatusResponse,
} from "@/types/reservation.types";
import type { CanvasData } from "@/types/topology.types";
import type { PaginatedResponse } from "@/types/pagination.types";
import apiClient from "./client";

function errorDetail(err: unknown, fallback: string): string {
  const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  return typeof detail === "string" ? detail : fallback;
}

async function fetchPaginatedReservations(
  skip = 0,
  limit = 50,
  all = false,
): Promise<PaginatedResponse<Reservation>> {
  // `all=true` is admin-only (the backend returns 403 for non-admins); it lists
  // every user's reservations instead of just the caller's own (issue #340).
  const resp = await apiClient.get<PaginatedResponse<Reservation>>("/reservations/", {
    params: all ? { skip, limit, all: true } : { skip, limit },
  });
  return resp.data;
}

async function fetchReservations(): Promise<Reservation[]> {
  const resp = await fetchPaginatedReservations(0, 500);
  return resp.items;
}

async function createReservation(data: ReservationCreate): Promise<Reservation> {
  const resp = await apiClient.post<Reservation>("/reservations/", data);
  return resp.data;
}

async function cancelReservation(id: string): Promise<void> {
  await apiClient.delete(`/reservations/${id}`);
}

async function releaseReservation(id: string): Promise<Reservation> {
  const resp = await apiClient.put<Reservation>(`/reservations/${id}/release`);
  return resp.data;
}

async function fetchCalendarReservations(params: CalendarQueryParams): Promise<Reservation[]> {
  const search = new URLSearchParams();
  search.set("range_start", params.range_start);
  search.set("range_end", params.range_end);
  if (params.status) {
    for (const s of params.status) {
      search.append("status", s);
    }
  }
  if (params.device_id) {
    search.set("device_id", params.device_id);
  }
  const resp = await apiClient.get<Reservation[]>(`/reservations/calendar?${search.toString()}`);
  return resp.data;
}

export function useCalendarReservations(params: CalendarQueryParams) {
  return useQuery({
    queryKey: ["reservations", "calendar", params],
    queryFn: () => fetchCalendarReservations(params),
  });
}

export function useReservations() {
  return useQuery({
    queryKey: ["reservations"],
    queryFn: fetchReservations,
  });
}

export function usePaginatedReservations(skip = 0, limit = 50, all = false) {
  return useQuery({
    queryKey: ["reservations", "paginated", skip, limit, all],
    queryFn: () => fetchPaginatedReservations(skip, limit, all),
    placeholderData: keepPreviousData,
  });
}

export function useCreateReservation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: createReservation,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["reservations"] }),
  });
}

export function useCancelReservation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: cancelReservation,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["reservations"] });
      toast.success("Reservation cancelled");
    },
    onError: (err) => toast.error(errorDetail(err, "Failed to cancel reservation")),
  });
}

export function useReleaseReservation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: releaseReservation,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["reservations"] });
      toast.success("Reservation released");
    },
    onError: (err) => toast.error(errorDetail(err, "Failed to release reservation")),
  });
}

async function updateReservation({ id, data }: { id: string; data: ReservationUpdate }): Promise<Reservation> {
  const resp = await apiClient.patch<Reservation>(`/reservations/${id}`, data);
  return resp.data;
}

export function useUpdateReservation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: updateReservation,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["reservations"] }),
  });
}

// --- Reservation fork (ADR 0006, issue #25 P3a) ---------------------------
// The fork endpoints hang off the reservation (owner-or-admin gated) and forward
// to cabling's internal fork routes. Live-edit mode reads, draft-saves, and
// commits the fork here instead of mutating the parent topology, so the parent's
// TopologyVersion history stays byte-for-byte unchanged.

export function forkKey(reservationId: string): (string | undefined)[] {
  return ["reservations", reservationId, "fork"];
}

async function fetchReservationFork(reservationId: string): Promise<ReservationFork> {
  const resp = await apiClient.get<ReservationFork>(`/reservations/${reservationId}/fork`);
  return resp.data;
}

// Loose-draft store: no version, no reconcile. Used by the debounced autosave.
export async function putForkCanvas(
  reservationId: string,
  canvasData: CanvasData,
): Promise<ForkCanvasDraftResult> {
  const resp = await apiClient.put<ForkCanvasDraftResult>(
    `/reservations/${reservationId}/fork/canvas`,
    { canvas_data: canvasData },
  );
  return resp.data;
}

// Reconcile-on-save: appends a fork version and returns the released/built delta.
export async function saveReservationFork(
  reservationId: string,
  canvasData: CanvasData,
): Promise<ForkSaveResult> {
  const resp = await apiClient.post<ForkSaveResult>(
    `/reservations/${reservationId}/fork/save`,
    { canvas_data: canvasData },
  );
  return resp.data;
}

export function useReservationFork(reservationId: string | null | undefined) {
  return useQuery({
    queryKey: forkKey(reservationId ?? ""),
    queryFn: () => fetchReservationFork(reservationId!),
    enabled: !!reservationId,
  });
}

export function useSaveReservationFork() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ reservationId, canvasData }: { reservationId: string; canvasData: CanvasData }) =>
      saveReservationFork(reservationId, canvasData),
    onSuccess: (_result, { reservationId }) => {
      // Refresh the fork so the History version list and status reflect the new
      // version. The canvas is NOT reloaded (the editor guards re-init), so an
      // in-progress edit is never stomped by the refetch.
      queryClient.invalidateQueries({ queryKey: forkKey(reservationId) });
    },
  });
}

// --- Per-connection L1 wiring status (ADR 0007, issue #345 P3b) ------------
// After a fork save reconciles the intended wiring, execution applies each L1
// cross-connect and records per-connection applied state. These owner-or-admin
// gated endpoints proxy execution's internal wiring-status and retry surface so
// the reservation detail can show which cross-connects landed, which FAILED, and
// (for ACTIVE reservations) reattempt the hardware-retryable ones.

export function wiringStatusKey(reservationId: string): (string | undefined)[] {
  return ["reservations", reservationId, "wiring-status"];
}

async function fetchReservationWiringStatus(
  reservationId: string,
): Promise<WiringStatusResponse> {
  const resp = await apiClient.get<WiringStatusResponse>(
    `/reservations/${reservationId}/wiring-status`,
  );
  return resp.data;
}

export async function retryReservationWiring(
  reservationId: string,
): Promise<WiringRetryResponse> {
  const resp = await apiClient.post<WiringRetryResponse>(
    `/reservations/${reservationId}/wiring/retry`,
  );
  return resp.data;
}

export function useReservationWiringStatus(reservationId: string | null | undefined) {
  return useQuery({
    queryKey: wiringStatusKey(reservationId ?? ""),
    queryFn: () => fetchReservationWiringStatus(reservationId!),
    enabled: !!reservationId,
  });
}

// Summarize a retry response's per-connection outcomes into the three counts the
// endpoint reports (reconnected / still_failed / not_retryable) for the toast.
export function summarizeWiringRetry(result: WiringRetryResponse): {
  reconnected: number;
  still_failed: number;
  not_retryable: number;
} {
  const counts = { reconnected: 0, still_failed: 0, not_retryable: 0 };
  for (const r of result.results) {
    if (r.outcome === "reconnected") counts.reconnected += 1;
    else if (r.outcome === "still_failed") counts.still_failed += 1;
    else if (r.outcome === "not_retryable") counts.not_retryable += 1;
  }
  return counts;
}

export function useRetryReservationWiring() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (reservationId: string) => retryReservationWiring(reservationId),
    onSuccess: (result, reservationId) => {
      const { reconnected, still_failed, not_retryable } = summarizeWiringRetry(result);
      const parts = [
        `${reconnected} reconnected`,
        `${still_failed} still failed`,
        `${not_retryable} not retryable`,
      ];
      const summary = `Retry complete: ${parts.join(", ")}`;
      if (still_failed === 0 && not_retryable === 0 && reconnected > 0) {
        toast.success(summary);
      } else {
        // Some rows did not recover; surface it without a hard-error toast so the
        // panel (refreshed below) shows the per-row detail.
        toast(summary);
      }
      // Refresh the panel so the per-connection rows reflect the new state.
      queryClient.invalidateQueries({ queryKey: wiringStatusKey(reservationId) });
    },
    onError: (err) => toast.error(errorDetail(err, "Failed to retry wiring")),
  });
}

// Narrow an axios error's response detail to the structured port-claim conflict
// (ADR 0006 Decision 4). Returns null for any other shape (a plain-string 409
// such as a non-ACTIVE or ARCHIVED refusal, or a non-409), so the caller can
// branch the conflict dialog from the plain error toast.
export function forkConflictDetail(err: unknown): ForkConflictDetail | null {
  const response = (err as { response?: { status?: number; data?: { detail?: unknown } } })
    ?.response;
  if (response?.status !== 409) return null;
  const detail = response.data?.detail;
  if (
    detail &&
    typeof detail === "object" &&
    Array.isArray((detail as { conflicts?: unknown }).conflicts)
  ) {
    return detail as ForkConflictDetail;
  }
  return null;
}
