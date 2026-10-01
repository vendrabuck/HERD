import type { Reservation, ReservationStatus } from "@/types/reservation.types";

/**
 * Single source of truth for which reservation statuses the UI offers Cancel
 * and Release on. These must match the backend's own gate exactly: never
 * stricter, never looser (issue #841).
 *
 * Cancel: `cancel_reservation` (services/reservations/app/services/
 * reservation_service.py) no-ops on COMPLETED, CANCELLED, and FAILED
 * (returning the reservation unchanged) and otherwise transitions to
 * CANCELLED, so the backend accepts cancel on every non-terminal
 * status: PENDING, PENDING_PROVISION, ACTIVE. COMPLETED/CANCELLED/FAILED are
 * deliberately excluded here even though the backend tolerates them
 * idempotently (204 with no state change): there is nothing left to cancel
 * on a reservation that already ended.
 *
 * Release: `release_reservation` (same file) no-ops unless status is
 * ACTIVE, so only ACTIVE reservations may be released.
 */
export function canCancel(status: ReservationStatus): boolean {
  return status === "PENDING" || status === "PENDING_PROVISION" || status === "ACTIVE";
}

export function canRelease(status: ReservationStatus): boolean {
  return status === "ACTIVE";
}

/**
 * Whether the admin "Classify now" action can get a 200 or a 503 from
 * `POST /reservations/admin/purpose-review/{id}/classify` (issue #822). It
 * mirrors `trigger_purpose_classify` in services/reservations/app/routers/
 * purpose_review.py, which 409s `not_eligible` while
 * `purpose_classify_requested_at` is null and 409s `already_suggested` once
 * `purpose_suggestion` is set (a dismissed or accepted suggestion stays set,
 * so it also refuses). `purpose_classify_requested_at` is stamped at the five
 * transitions into COMPLETED, CANCELLED, or FAILED and by the admin backfill,
 * and the API does not expose it, so terminal status is the closest client
 * signal. The one gap is a reservation that went terminal before the stamp
 * existed and was never backfilled: the button shows and the backend answers
 * 409 `not_eligible`, which the caller handles. A confirmed
 * `purpose_category` changes nothing on the backend, so it is not read here.
 * The admin-role check stays with the caller (`isAdminRole`).
 */
export function canClassifyPurpose(
  reservation: Pick<Reservation, "status" | "purpose_suggestion">,
): boolean {
  const terminal =
    reservation.status === "COMPLETED" ||
    reservation.status === "CANCELLED" ||
    reservation.status === "FAILED";
  return terminal && !reservation.purpose_suggestion;
}

/**
 * Who may cancel or release, in one place (issue #843). The detail modal and
 * the Reservations page's bulk actions both ask here, so a row is offered the
 * action by exactly the same rule everywhere: the caller must own the
 * reservation AND the status gate above must pass. The backend is looser for
 * an admin cancel (issue #340); the UI keeps the owner-only rule the detail
 * modal always had, by decision, and an admin's cross-owner cancel stays on
 * the API.
 */
export function canCancelAs(
  reservation: Pick<Reservation, "status" | "user_id">,
  userId: string | undefined | null,
): boolean {
  return !!userId && userId === reservation.user_id && canCancel(reservation.status);
}

export function canReleaseAs(
  reservation: Pick<Reservation, "status" | "user_id">,
  userId: string | undefined | null,
): boolean {
  return !!userId && userId === reservation.user_id && canRelease(reservation.status);
}
