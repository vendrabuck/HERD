/**
 * Client-side label formatting for reservation purpose classification (issue
 * #646 phase 1). The taxonomy is server-configurable (GET
 * /reservations/purpose-categories, env override PURPOSE_CATEGORIES on the
 * backend), so this file never hardcodes the list for validation, only a
 * nicer label for the seven shipped defaults plus a snake_case humanizer for
 * anything else: an admin-added category, or a value a past reservation still
 * carries after it was removed from the list.
 */

// The literal bucket key the reporting endpoint uses for a null category
// (GET /reservations/reports/utilization). A reservation's own
// `purpose_category` field uses null instead; both mean the same thing.
export const UNCLASSIFIED_CATEGORY = "unclassified";

const DEFAULT_LABELS: Record<string, string> = {
  qa_regression: "QA and regression",
  support_case_replication: "Support case replication",
  feature_development: "Feature development",
  customer_demo_poc: "Customer demo or POC",
  training: "Training",
  performance_benchmark: "Performance benchmark",
  other: "Other",
};

export function isUnclassifiedCategory(value: string | null | undefined): boolean {
  return !value || value === UNCLASSIFIED_CATEGORY;
}

export function humanizePurposeCategory(value: string): string {
  return value
    .split("_")
    .filter(Boolean)
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(" ");
}

export function purposeCategoryLabel(value: string | null | undefined): string {
  if (isUnclassifiedCategory(value)) return "Unclassified";
  return DEFAULT_LABELS[value as string] ?? humanizePurposeCategory(value as string);
}
