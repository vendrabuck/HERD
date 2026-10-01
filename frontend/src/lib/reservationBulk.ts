import { canCancelAs, canReleaseAs } from "@/lib/reservationStatus";
import type { Reservation } from "@/types/reservation.types";

/**
 * Pure logic behind the Reservations page's multi-select Cancel and Release
 * (issue #843). The page fans out over the per-id endpoints, so everything a
 * test needs to pin lives here: who is eligible, why the rest are not, how a
 * settled fan-out changes the selection, and the sentences shown to the user.
 */

export type BulkAction = "cancel" | "release";

// finished: the reservation already ended (COMPLETED, CANCELLED, FAILED).
// not_active: Release only, a live reservation that is not ACTIVE yet.
// not_yours: the caller does not own it.
export type SkipReason = "finished" | "not_active" | "not_yours";

export interface SkippedReservation {
  reservation: Reservation;
  reason: SkipReason;
}

export interface SelectionPartition {
  eligible: Reservation[];
  skipped: SkippedReservation[];
}

const FINISHED_STATUSES = ["COMPLETED", "CANCELLED", "FAILED"];

function skipReason(action: BulkAction, reservation: Reservation, userId: string | null | undefined) {
  const allowed =
    action === "cancel"
      ? canCancelAs(reservation, userId)
      : canReleaseAs(reservation, userId);
  if (allowed) return null;
  // Ownership first: a row the caller may not touch is "not yours" whatever
  // its status, so the reason never hints at another user's reservation state.
  if (!userId || userId !== reservation.user_id) return "not_yours" as const;
  return FINISHED_STATUSES.includes(reservation.status)
    ? ("finished" as const)
    : ("not_active" as const);
}

/**
 * Split a selection into the rows the action will run on and the rows it will
 * not, with a reason per skipped row. Per row, never per batch, and through the
 * same gates the single-row buttons use.
 */
export function partitionSelection(
  action: BulkAction,
  selected: readonly Reservation[],
  userId: string | null | undefined,
): SelectionPartition {
  const eligible: Reservation[] = [];
  const skipped: SkippedReservation[] = [];
  for (const reservation of selected) {
    const reason = skipReason(action, reservation, userId);
    if (reason === null) eligible.push(reservation);
    else skipped.push({ reservation, reason });
  }
  return { eligible, skipped };
}

const REASON_ORDER: SkipReason[] = ["finished", "not_active", "not_yours"];
const REASON_TEXT: Record<SkipReason, string> = {
  finished: "already finished",
  not_active: "not active",
  not_yours: "not yours",
};

/** "1 already finished, 2 not yours": counts per reason, fixed order. */
export function describeSkipped(skipped: readonly SkippedReservation[]): string {
  return REASON_ORDER.map((reason) => ({
    reason,
    n: skipped.filter((s) => s.reason === reason).length,
  }))
    .filter(({ n }) => n > 0)
    .map(({ reason, n }) => `${n} ${REASON_TEXT[reason]}`)
    .join(", ");
}

function plural(n: number): string {
  return `${n} reservation${n === 1 ? "" : "s"}`;
}

const VERB: Record<BulkAction, { present: string; past: string }> = {
  cancel: { present: "Cancel", past: "Cancelled" },
  release: { present: "Release", past: "Released" },
};

export function confirmTitle(action: BulkAction): string {
  return action === "cancel" ? "Cancel Reservations" : "Release Reservations";
}

export function confirmLabel(action: BulkAction, partition: SelectionPartition): string {
  return `${VERB[action].present} ${plural(partition.eligible.length)}`;
}

export function keepLabel(action: BulkAction): string {
  return action === "cancel" ? "Keep reservations" : "Do not release";
}

const CONSEQUENCE: Record<BulkAction, string> = {
  cancel: "This releases their devices and cannot be undone.",
  release: "This ends them early and frees their devices.",
};

/**
 * The confirmation text: how many will be acted on, how many will not and why.
 * Never called with nothing eligible (the action is disabled then).
 */
export function confirmDescription(action: BulkAction, partition: SelectionPartition): string {
  const verb = VERB[action].present;
  const { eligible, skipped } = partition;
  const total = eligible.length + skipped.length;
  if (skipped.length === 0) {
    return `${verb} ${plural(eligible.length)}? ${CONSEQUENCE[action]}`;
  }
  const skippedText = `${skipped.length} will be skipped: ${describeSkipped(skipped)}.`;
  return `${verb} ${eligible.length} of the ${total} selected reservations? ${CONSEQUENCE[action]} ${skippedText}`;
}

/** Why an action is disabled when no selected row is eligible. */
export function noneEligibleMessage(
  action: BulkAction,
  skipped: readonly SkippedReservation[],
): string {
  const past = VERB[action].past.toLowerCase();
  return `None of the selected reservations can be ${past}: ${describeSkipped(skipped)}.`;
}

export interface BulkOutcome {
  /** Ids still selected: failures, plus any id that got no result. */
  remaining: Set<string>;
  succeeded: number;
  /** Failed or unanswered ids; reason is the server's text, null when unknown. */
  failures: { id: string; reason: string | null }[];
}

/**
 * Fold a Promise.allSettled result over `ids` (same order) into the selection.
 * A fulfilled row leaves the selection; a rejected row stays, with the server's
 * reason; an id with no result is KEPT and counted as a failure, never assumed
 * done (the applyBulkResult rule in bulkStaging.ts).
 */
export function applySettled(
  selection: ReadonlySet<string>,
  ids: readonly string[],
  results: readonly PromiseSettledResult<unknown>[],
  reasonOf: (err: unknown) => string | null,
): BulkOutcome {
  const remaining = new Set(selection);
  const failures: BulkOutcome["failures"] = [];
  let succeeded = 0;
  ids.forEach((id, i) => {
    const result = results[i];
    if (result && result.status === "fulfilled") {
      remaining.delete(id);
      succeeded += 1;
    } else {
      failures.push({ id, reason: result ? reasonOf(result.reason) : null });
    }
  });
  return { remaining, succeeded, failures };
}

/** The one reason every failure shares, or null when they differ or are unknown. */
export function sharedReason(failures: BulkOutcome["failures"]): string | null {
  if (failures.length === 0) return null;
  const first = failures[0].reason;
  if (first === null || first === "") return null;
  return failures.every((f) => f.reason === first) ? first : null;
}

/** The toast sentence: "Cancelled 2, failed 1: reason" or "Cancelled 3 reservations". */
export function summarizeOutcome(action: BulkAction, outcome: BulkOutcome): string {
  const past = VERB[action].past;
  if (outcome.failures.length === 0) return `${past} ${plural(outcome.succeeded)}`;
  const base = `${past} ${outcome.succeeded}, failed ${outcome.failures.length}`;
  const reason = sharedReason(outcome.failures);
  return reason ? `${base}: ${reason}` : base;
}
