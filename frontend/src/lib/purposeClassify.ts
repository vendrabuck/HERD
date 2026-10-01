import { purposeCategoryLabel } from "@/lib/purposeCategories";
import type { PurposeClassification, PurposeClassifyOutcome } from "@/types/reservation.types";

// User-facing wording for the admin "Classify now" action (issue #822), kept
// in one place so the modal and its tests read the same sentences.

export const PURPOSE_CLASSIFY_DISABLED_MESSAGE = "Purpose classification is disabled.";

export const PURPOSE_CLASSIFY_ALREADY_SUGGESTED_MESSAGE =
  "This reservation already has a suggestion. Review it on the Purpose Review page.";

export const PURPOSE_CLASSIFY_NOT_ELIGIBLE_MESSAGE =
  "This reservation is not ready to be classified.";

// Every non-ok outcome of a 200 response. `ok` is handled by
// `purposeClassifySuccessMessage`, so it is not in this map.
export const PURPOSE_CLASSIFY_OUTCOME_MESSAGES: Record<
  Exclude<PurposeClassifyOutcome, "ok">,
  string
> = {
  timeout: "Classification timed out. Check that the AI orchestrator is responding, then try again.",
  transient:
    "The AI orchestrator could not take the request right now. Check its status, then try again.",
  failed: "Classification failed. Check the AI orchestrator logs, then try again.",
  forbidden:
    "The AI orchestrator refused the request. Check that its internal token matches this service.",
};

export function purposeClassifySuccessMessage(suggestion: PurposeClassification | null): string {
  return suggestion
    ? `Suggested category: ${purposeCategoryLabel(suggestion.top_category)}.`
    : "Classification finished.";
}
