import { canCancel, canClassifyPurpose, canRelease } from "@/lib/reservationStatus";
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

// Issue #822: mirrors trigger_purpose_classify (services/reservations/app/
// routers/purpose_review.py). Eligible means terminal (the proxy for the
// unexposed purpose_classify_requested_at stamp) and no suggestion yet; a
// dismissed or accepted suggestion stays set, so it refuses like any other.
const SUGGESTION = {
  distribution: [{ category: "qa_regression", probability: 0.9 }],
  top_category: "qa_regression",
  pass: "end",
  model: "m",
  rationale: "r",
  generated_at: "2026-06-02T01:00:00Z",
  signals_used: ["purpose_text"],
};

const ALL_STATUSES: ReservationStatus[] = [
  "PENDING",
  "PENDING_PROVISION",
  "ACTIVE",
  "COMPLETED",
  "CANCELLED",
  "FAILED",
];
const TERMINAL = new Set<ReservationStatus>(["COMPLETED", "CANCELLED", "FAILED"]);

describe("canClassifyPurpose", () => {
  describe.each([
    { label: "suggestion null", purpose_suggestion: null, hasSuggestion: false },
    { label: "suggestion absent", purpose_suggestion: undefined, hasSuggestion: false },
    { label: "suggestion set", purpose_suggestion: SUGGESTION, hasSuggestion: true },
  ])("$label", ({ purpose_suggestion, hasSuggestion }) => {
    it.each(ALL_STATUSES)("status %s", (status) => {
      expect(canClassifyPurpose({ status, purpose_suggestion })).toBe(
        TERMINAL.has(status) && !hasSuggestion,
      );
    });
  });
});
