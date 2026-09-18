import { canCancel, canRelease } from "@/lib/reservationStatus";
import type { ReservationStatus } from "@/types/reservation.types";

// Table-driven against every ReservationStatus value (issue #841): the UI
// gate must match the backend's cancel_reservation/release_reservation rule
// exactly (services/reservations/app/services/reservation_service.py:2286
// and :2382), never stricter and never looser.
const CASES: { status: ReservationStatus; cancel: boolean; release: boolean }[] = [
  { status: "PENDING", cancel: true, release: false },
  { status: "PENDING_PROVISION", cancel: true, release: false },
  { status: "ACTIVE", cancel: true, release: true },
  { status: "COMPLETED", cancel: false, release: false },
  { status: "CANCELLED", cancel: false, release: false },
  { status: "FAILED", cancel: false, release: false },
];

describe("canCancel", () => {
  it.each(CASES)("$status -> canCancel $cancel", ({ status, cancel }) => {
    expect(canCancel(status)).toBe(cancel);
  });
});

describe("canRelease", () => {
  it.each(CASES)("$status -> canRelease $release", ({ status, release }) => {
    expect(canRelease(status)).toBe(release);
  });
});
