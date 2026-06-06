interface LiveEditBarProps {
  deviceCount: number;
  invalidEdgeCount: number;
  isCommitting: boolean;
  onCommit: () => void;
  onCancel: () => void;
}

// Shown when the topology editor is opened bound to a live reservation
// (/topology/:id?reservationId=...). Editing the canvas here re-wires a running
// reservation: committing PUTs the canvas and PATCHes the reservation's device
// set, which the backend turns into incremental provisioning. Commit is blocked
// while any edge has no physical path, matching the server-side validation.
export function LiveEditBar({
  deviceCount,
  invalidEdgeCount,
  isCommitting,
  onCommit,
  onCancel,
}: LiveEditBarProps) {
  const blocked = invalidEdgeCount > 0;
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
          </div>
          <p className="text-xs text-gray-600 mt-1">
            Changes commit to the running reservation and re-provision the
            affected devices.
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
            disabled={blocked || isCommitting}
            title={
              blocked
                ? `Cannot commit: ${invalidEdgeCount} edge${invalidEdgeCount !== 1 ? "s" : ""} have no physical path`
                : undefined
            }
            className="text-sm px-3 py-1 rounded text-white bg-blue-600 hover:bg-blue-700 disabled:opacity-40 disabled:cursor-not-allowed"
          >
            {isCommitting ? "Committing..." : "Commit to reservation"}
          </button>
        </div>
      </div>
    </div>
  );
}
