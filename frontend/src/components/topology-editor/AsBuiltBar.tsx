interface AsBuiltBarProps {
  deviceCount: number;
  onClose: () => void;
}

// Shown when the fork editor is opened for a reservation that has ended (ADR 0006
// Decision 6). The fork is ARCHIVED, so the canvas is read-only: it renders the
// immutable as-built record of what the reservation was last reconciled to. No
// commit; the only action is to return to the reservations list.
export function AsBuiltBar({ deviceCount, onClose }: AsBuiltBarProps) {
  return (
    <div className="absolute top-4 left-1/2 -translate-x-1/2 z-10 bg-white border-2 border-gray-400 rounded-lg shadow-lg px-4 py-3 max-w-2xl">
      <div className="flex items-start gap-4">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <span className="text-xs font-bold px-1.5 py-0.5 rounded bg-gray-700 text-white">
              AS-BUILT RECORD (READ-ONLY)
            </span>
            <span className="text-sm font-medium text-gray-900">
              {deviceCount} device{deviceCount !== 1 ? "s" : ""}
            </span>
          </div>
          <p className="text-xs text-gray-600 mt-1">
            This reservation has ended. The wiring below is the archived as-built record and
            cannot be edited.
          </p>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <button
            onClick={onClose}
            className="text-sm px-3 py-1 rounded text-gray-700 hover:bg-gray-100 border border-gray-300"
          >
            Back to reservations
          </button>
        </div>
      </div>
    </div>
  );
}
