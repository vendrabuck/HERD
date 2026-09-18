import type { ReservationStatus } from "@/types/reservation.types";

/**
 * Single source of truth for which reservation statuses the UI offers Cancel
 * and Release on. These must match the backend's own gate exactly: never
 * stricter, never looser (issue #841).
 *
 * Cancel: `cancel_reservation` (services/reservations/app/services/
 * reservation_service.py:2286) no-ops on COMPLETED, CANCELLED, and FAILED
 * (2313-2317, returning the reservation unchanged) and otherwise transitions
 * to CANCELLED (2319), so the backend accepts cancel on every non-terminal
 * status: PENDING, PENDING_PROVISION, ACTIVE. COMPLETED/CANCELLED/FAILED are
 * deliberately excluded here even though the backend tolerates them
 * idempotently (204 with no state change): there is nothing left to cancel
 * on a reservation that already ended.
 *
 * Release: `release_reservation` (same file, 2382) no-ops unless status is
 * ACTIVE (2391-2392), so only ACTIVE reservations may be released.
 */
export function canCancel(status: ReservationStatus): boolean {
  return status === "PENDING" || status === "PENDING_PROVISION" || status === "ACTIVE";
}

export function canRelease(status: ReservationStatus): boolean {
  return status === "ACTIVE";
}
