import { cn } from "@/lib/cn";
import { loadFailureDetail } from "@/lib/errors";

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
