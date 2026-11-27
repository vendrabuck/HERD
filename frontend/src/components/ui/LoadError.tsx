import { cn } from "@/lib/cn";
import { loadFailureDetail } from "@/lib/errors";
import { refreshFailed } from "@/lib/loadState";

/** The slice of a TanStack Query result `LoadErrorNotice` reads. */
export interface RefreshableQuery<T = unknown> {
  data: T | undefined;
  isError: boolean;
  error: unknown;
  isFetching: boolean;
  refetch: () => unknown;
}

/**
 * The notice for a refresh that failed while the page still shows an earlier
 * answer (issue #1320, `refreshFailed`): the content stays, and above it a
 * compact strip says `Could not refresh <what>.` plus the reason, with Retry.
 * Renders nothing in every other state, so a page places it once above its
 * content. `role="status"`: the page still works, so it is announced politely.
 */
export function LoadErrorNotice<T>({
  what,
  query,
  isEmpty,
  isValid,
  className,
}: {
  what: string;
  query: RefreshableQuery<T>;
  /** The same predicates the page passes to `loadState`. */
  isEmpty?: (data: T) => boolean;
  isValid?: (data: T) => boolean;
  className?: string;
}) {
  if (!refreshFailed(query, isEmpty, isValid)) return null;
  const retrying = query.isFetching;
  return (
    <div
      role="status"
      className={cn(
        "flex items-center justify-between gap-4 px-3 py-2 rounded-lg border border-amber-200 bg-amber-50 text-xs text-amber-800",
        className,
      )}
    >
      <p>
        Could not refresh {what}. {loadFailureDetail(query.error)}
      </p>
      <button
        type="button"
        onClick={() => {
          if (!retrying) void query.refetch();
        }}
        aria-disabled={retrying || undefined}
        aria-label={retrying ? undefined : "Retry loading " + what}
        className="shrink-0 px-2 py-1 text-xs font-medium text-amber-800 bg-white border border-amber-300 rounded hover:bg-amber-100 aria-disabled:opacity-50 transition-colors"
      >
        {retrying ? "Retrying..." : "Retry"}
      </button>
    </div>
  );
}

/**
 * The ONE error state for a load that failed (issue #1320): a red alert strip
 * that says what could not be loaded, then the server's reason through
 * `loadFailureDetail` (rendered as text), with a Retry button. A page shows
 * it in place of the list, record, or form the load would have filled, never
 * an empty state or an empty editable form.
 *
 * `role="alert"` announces it when it appears. While `retrying` the button
 * reads "Retrying..." and ignores clicks; it is not `disabled`, so keyboard
 * focus stays on it.
 */
export function LoadError({
  what,
  error,
  onRetry,
  retrying = false,
  className,
}: {
  /** The thing that failed to load, as it reads after "Could not load ". */
  what: string;
  error: unknown;
  onRetry: () => void;
  retrying?: boolean;
  className?: string;
}) {
  return (
    <div
      role="alert"
      className={cn(
        "flex items-center justify-between gap-4 px-4 py-3 rounded-lg border border-red-200 bg-red-50 text-sm text-red-700",
        className,
      )}
    >
      <p>
        Could not load {what}. {loadFailureDetail(error)}
      </p>
      <button
        type="button"
        onClick={() => {
          if (!retrying) onRetry();
        }}
        aria-disabled={retrying || undefined}
        aria-label={retrying ? undefined : "Retry loading " + what}
        className="shrink-0 px-3 py-1.5 text-sm font-medium text-red-700 bg-white border border-red-300 rounded hover:bg-red-100 aria-disabled:opacity-50 transition-colors"
      >
        {retrying ? "Retrying..." : "Retry"}
      </button>
    </div>
  );
}
