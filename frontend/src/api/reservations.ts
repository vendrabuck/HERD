import { useMutation, useQuery, useQueryClient, keepPreviousData } from "@tanstack/react-query";
import toast from "react-hot-toast";
import type { CalendarQueryParams, Reservation, ReservationCreate, ReservationUpdate } from "@/types/reservation.types";
import type { PaginatedResponse } from "@/types/pagination.types";
import apiClient from "./client";

function errorDetail(err: unknown, fallback: string): string {
  const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  return typeof detail === "string" ? detail : fallback;
}

async function fetchPaginatedReservations(skip = 0, limit = 50): Promise<PaginatedResponse<Reservation>> {
  const resp = await apiClient.get<PaginatedResponse<Reservation>>("/reservations/", {
    params: { skip, limit },
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

export function usePaginatedReservations(skip = 0, limit = 50) {
  return useQuery({
    queryKey: ["reservations", "paginated", skip, limit],
    queryFn: () => fetchPaginatedReservations(skip, limit),
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
