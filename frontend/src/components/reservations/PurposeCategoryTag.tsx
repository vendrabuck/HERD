import { isUnclassifiedCategory, purposeCategoryLabel } from "@/lib/purposeCategories";

/**
 * Small tag rendering a reservation's purpose category (issue #646 phase 1).
 * Accepts either a reservation's own `purpose_category` (null when unset) or
 * a reporting bucket's raw key (the literal string "unclassified"): both are
 * handled by purposeCategoryLabel/isUnclassifiedCategory. Deliberately muted
 * for the unclassified case so a fleet of unclassified reservations reads as
 * an absence of data, not as a category of its own.
 */
export function PurposeCategoryTag({ category }: { category: string | null | undefined }) {
  const unclassified = isUnclassifiedCategory(category);
  return (
    <span
      className={`text-xs px-1.5 py-0.5 rounded ${
        unclassified ? "bg-gray-50 text-gray-400" : "bg-blue-50 text-blue-700"
      }`}
    >
      {purposeCategoryLabel(category)}
    </span>
  );
}
