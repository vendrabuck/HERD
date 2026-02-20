import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { Reservation, ReservationCreate } from "@/types/reservation.types";
import apiClient from "./client";

async function fetchReservations(): Promise<Reservation[]> {
  const resp = await apiClient.get<Reservation[]>("/reservations/");
  return resp.data;
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

export function useReservations() {
  return useQuery({
    queryKey: ["reservations"],
    queryFn: fetchReservations,
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
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["reservations"] }),
  });
}

export function useReleaseReservation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: releaseReservation,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["reservations"] }),
  });
}
