import type { ForkAutosaveStatus } from "@/hooks/useForkAutosave";

interface LiveEditBarProps {
  deviceCount: number;
  invalidEdgeCount: number;
  isCommitting: boolean;
  // False until the reservation's fork has been fetched and hydrated onto the
  // canvas (TopologyEditorPage's forkLoaded state). Committing before then
  // would save whatever is currently in the store, which is an empty canvas
  // in the hydration window: the fork save would release live wiring and the
  // device-set PATCH would send an empty device_ids, which the reservations
  // schema rejects with 422. The button stays disabled until this is true.
  forkLoaded: boolean;
  autosaveStatus?: ForkAutosaveStatus;
  onCommit: () => void;
  onCancel: () => void;
}

const AUTOSAVE_LABEL: Record<ForkAutosaveStatus, string> = {
  idle: "Draft up to date",
  saving: "Saving draft...",
  saved: "Draft saved",
  error: "Draft save failed",
};

// Shown when the topology editor is opened bound to a live (ACTIVE) reservation
// (/topology/:id?reservationId=...). Editing the canvas here re-wires the
// reservation's fork: edits autosave as loose drafts, and committing reconciles
// the fork (POST /reservations/{id}/fork/save, which appends a fork version and
// leaves the parent topology's history untouched) and PATCHes the reservation's
// device set to drive incremental provisioning. Commit is blocked while any edge
// has no physical path, matching the server-side validation.
export function LiveEditBar({
  deviceCount,
  invalidEdgeCount,
  isCommitting,
  forkLoaded,
  autosaveStatus,
  onCommit,
  onCancel,
}: LiveEditBarProps) {
  const blocked = invalidEdgeCount > 0;
  const commitLabel = isCommitting
    ? "Committing..."
    : !forkLoaded
      ? "Loading fork..."
      : "Commit to reservation";
  return (
    <div className="absolute top-4 left-1/2 -translate-x-1/2 z-10 bg-white border-2 border-blue-400 rounded-lg shadow-lg px-4 py-3 max-w-2xl">
      <div className="flex items-start gap-4">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <span className="text-xs font-bold px-1.5 py-0.5 rounded bg-blue-600 text-white">
              EDITING LIVE RESERVATION
            </span>
            <span className="text-sm font-medium text-gray-900">
              {deviceCount} device{deviceCount !== 1 ? "s" : ""}
            </span>
            {autosaveStatus && (
              <span
                className={`text-xs ${
                  autosaveStatus === "error" ? "text-red-600" : "text-gray-400"
                }`}
                aria-live="polite"
              >
                {AUTOSAVE_LABEL[autosaveStatus]}
              </span>
            )}
          </div>
          <p className="text-xs text-gray-600 mt-1">
            Changes commit to the fork and reconcile the wiring to hardware.
            Edits autosave as drafts; committing saves a fork version and
            leaves the master topology untouched.
          </p>
          {blocked && (
            <p className="text-xs text-red-600 mt-1">
              {invalidEdgeCount} edge{invalidEdgeCount !== 1 ? "s" : ""} have no
              physical path; fix or remove them before committing.
            </p>
          )}
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <button
            onClick={onCancel}
            disabled={isCommitting}
            className="text-sm px-3 py-1 rounded text-gray-700 hover:bg-gray-100 border border-gray-300 disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            onClick={onCommit}
            disabled={blocked || isCommitting || !forkLoaded}
            title={
              blocked
                ? `Cannot commit: ${invalidEdgeCount} edge${invalidEdgeCount !== 1 ? "s" : ""} have no physical path`
                : undefined
            }
            className="text-sm px-3 py-1 rounded text-white bg-blue-600 hover:bg-blue-700 disabled:opacity-40 disabled:cursor-not-allowed"
          >
            {commitLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
