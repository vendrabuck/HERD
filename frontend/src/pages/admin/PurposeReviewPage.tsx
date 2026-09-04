import { useMemo, useState } from "react";
import toast from "react-hot-toast";
import {
  useAcceptPurposeSuggestion,
  useBackfillPurposeClassification,
  useDismissPurposeSuggestion,
  usePurposeCategories,
  usePurposeReview,
} from "@/api/reservations";
import { useAllUsers } from "@/api/admin";
import { Card } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { SkeletonRows } from "@/components/ui/Skeleton";
import { Pagination } from "@/components/ui/Pagination";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { PurposeCategoryTag } from "@/components/reservations/PurposeCategoryTag";
import { purposeCategoryLabel } from "@/lib/purposeCategories";
import { errorDetail } from "@/lib/errors";
import type { PurposeReviewItem } from "@/types/reservation.types";

// Admin review surface for AI purpose suggestions (issue #646 phase 2, ADR
// 0013 point 10). Lists reservations that carry a suggestion still awaiting
// accept/dismiss, grouped by top suggested category.

const PAGE_SIZE = 20;

export function PurposeReviewPage() {
  const [skip, setSkip] = useState(0);
  const [categoryFilter, setCategoryFilter] = useState("");
  // Optimistic per-row removal: a row hides the instant its action is
  // clicked, before the server confirms, and comes back if the call fails.
  const [removedIds, setRemovedIds] = useState<Record<string, boolean>>({});

  const { data, isLoading, isError } = usePurposeReview(
    skip,
    PAGE_SIZE,
    categoryFilter || undefined,
  );
  const { data: categoriesData } = usePurposeCategories();
  const categories = categoriesData?.categories ?? [];
  const { data: users } = useAllUsers();
  const userLabel = (id: string): string => {
    const match = users?.find((u) => u.id === id);
    return match ? match.username : id.slice(0, 8);
  };

  const accept = useAcceptPurposeSuggestion();
  const dismiss = useDismissPurposeSuggestion();
  const backfill = useBackfillPurposeClassification();

  const items = (data?.items ?? []).filter((item) => !removedIds[item.reservation_id]);
  const total = data?.total ?? 0;

  const groups = useMemo(() => {
    const byCategory = new Map<string, PurposeReviewItem[]>();
    for (const item of items) {
      const key = item.purpose_suggestion?.top_category ?? "unknown";
      const list = byCategory.get(key);
      if (list) list.push(item);
      else byCategory.set(key, [item]);
    }
    return Array.from(byCategory.entries())
      .map(([category, rows]) => ({ category, rows }))
      .sort((a, b) => b.rows.length - a.rows.length);
  }, [items]);

  const hide = (id: string) => setRemovedIds((prev) => ({ ...prev, [id]: true }));
  const restore = (id: string) =>
    setRemovedIds((prev) => {
      const next = { ...prev };
      delete next[id];
      return next;
    });

  const handleAccept = async (id: string, purposeCategory: string | null) => {
    hide(id);
    try {
      await accept.mutateAsync({ id, purposeCategory });
      toast.success("Accepted");
    } catch (err) {
      restore(id);
      toast.error(errorDetail(err, "Failed to accept suggestion"));
    }
  };

  const handleDismiss = async (id: string) => {
    hide(id);
    try {
      await dismiss.mutateAsync(id);
      toast.success("Dismissed");
    } catch (err) {
      restore(id);
      toast.error(errorDetail(err, "Failed to dismiss suggestion"));
    }
  };

  const handleBackfill = async () => {
    try {
      const result = await backfill.mutateAsync();
      toast.success(`Marked ${result.marked} reservations for classification`);
    } catch (err) {
      toast.error(errorDetail(err, "Failed to classify history"));
    }
  };

  return (
    <div className="h-full overflow-y-auto">
      <div className="px-6 xl:px-12 2xl:px-16 py-8 space-y-4">
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-semibold text-gray-900">Purpose Review</h2>
          <button
            type="button"
            onClick={handleBackfill}
            disabled={backfill.isPending}
            className="text-sm px-3 py-1.5 rounded bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50"
          >
            {backfill.isPending ? "Classifying..." : "Classify history"}
          </button>
        </div>

        <div className="flex items-center gap-2">
          <label htmlFor="purpose-review-category" className="text-sm text-gray-600">
            Category
          </label>
          <select
            id="purpose-review-category"
            value={categoryFilter}
            onChange={(e) => {
              setCategoryFilter(e.target.value);
              setSkip(0);
            }}
            className="border border-gray-300 rounded-lg px-2 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
          >
            <option value="">All</option>
            {categories.map((c) => (
              <option key={c} value={c}>
                {purposeCategoryLabel(c)}
              </option>
            ))}
          </select>
        </div>

        {isLoading ? (
          <Card>
            <SkeletonRows rows={5} />
          </Card>
        ) : isError ? (
          <Card>
            <EmptyState>Failed to load the review queue</EmptyState>
          </Card>
        ) : items.length === 0 ? (
          <Card>
            <EmptyState>Nothing to review</EmptyState>
          </Card>
        ) : (
          <div className="space-y-3">
            {groups.map((group) => (
              <details
                key={group.category}
                open
                className="bg-white rounded-lg border border-gray-200 shadow-sm overflow-hidden"
              >
                <summary className="px-4 py-3 border-b border-gray-200 cursor-pointer text-sm font-semibold text-gray-900 flex items-center justify-between list-none">
                  <span>
                    {group.category === "unknown"
                      ? "No suggestion"
                      : purposeCategoryLabel(group.category)}
                  </span>
                  <span className="text-xs font-normal text-gray-500">{group.rows.length}</span>
                </summary>
                <ul className="divide-y divide-gray-100">
                  {group.rows.map((item) => (
                    <PurposeReviewRow
                      key={item.reservation_id}
                      item={item}
                      categories={categories}
                      userLabel={userLabel(item.user_id)}
                      onAccept={(category) => handleAccept(item.reservation_id, category)}
                      onDismiss={() => handleDismiss(item.reservation_id)}
                    />
                  ))}
                </ul>
              </details>
            ))}
          </div>
        )}

        <Pagination total={total} skip={skip} limit={PAGE_SIZE} onPageChange={setSkip} />
      </div>
    </div>
  );
}

function PurposeReviewRow({
  item,
  categories,
  userLabel,
  onAccept,
  onDismiss,
}: {
  item: PurposeReviewItem;
  categories: string[];
  userLabel: string;
  onAccept: (category: string | null) => void;
  onDismiss: () => void;
}) {
  const distribution = item.purpose_suggestion?.distribution.slice(0, 3) ?? [];
  const rationale = item.purpose_suggestion?.rationale;
  const start = new Date(item.start_time).toLocaleDateString();
  const end = new Date(item.end_time).toLocaleDateString();

  return (
    <li className="px-4 py-3 space-y-2">
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="text-sm text-gray-900">{item.purpose || "-"}</p>
          <p className="text-xs text-gray-500">
            {userLabel} - {start} to {end} - {item.device_count} device
            {item.device_count === 1 ? "" : "s"}
          </p>
        </div>
        <div className="flex items-center gap-1.5 shrink-0">
          <StatusBadge status={item.status} />
          {item.purpose_category && <PurposeCategoryTag category={item.purpose_category} />}
        </div>
      </div>
      {distribution.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5" title={rationale}>
          {distribution.map((d, i) => (
            <span
              key={d.category}
              className={`text-xs px-1.5 py-0.5 rounded ${
                i === 0 ? "bg-blue-100 text-blue-800 font-medium" : "bg-blue-50 text-blue-700"
              }`}
            >
              {purposeCategoryLabel(d.category)} {Math.round(d.probability * 100)}%
            </span>
          ))}
        </div>
      )}
      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={() => onAccept(null)}
          className="text-xs px-2.5 py-1 rounded bg-blue-600 text-white hover:bg-blue-700"
        >
          Accept
        </button>
        <select
          aria-label="Accept a different category"
          defaultValue=""
          onChange={(e) => {
            if (e.target.value) onAccept(e.target.value);
          }}
          className="text-xs border border-gray-300 rounded px-1.5 py-1 focus:outline-none focus:ring-2 focus:ring-blue-500"
        >
          <option value="">Accept as...</option>
          {categories.map((c) => (
            <option key={c} value={c}>
              {purposeCategoryLabel(c)}
            </option>
          ))}
        </select>
        <button
          type="button"
          onClick={onDismiss}
          className="text-xs px-2.5 py-1 rounded border border-gray-300 text-gray-600 hover:bg-gray-50"
        >
          Dismiss
        </button>
      </div>
    </li>
  );
}
