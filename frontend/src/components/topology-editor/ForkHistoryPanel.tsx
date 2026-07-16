import type { ForkVersionSummary } from "@/types/reservation.types";

interface ForkHistoryPanelProps {
  versions: ForkVersionSummary[];
  onClose: () => void;
}

function formatDate(iso: string): string {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

// The version history for a reservation fork (ADR 0006 Decision 6, issue #25
// P3a). Each fork save appends a version; this lists them newest-first, reusing
// the parent-topology HistoryPanel's visual pattern. It is read-only: P3a ships
// no fork version preview/diff/restore endpoints, so there are no per-version
// actions here.
export function ForkHistoryPanel({ versions, onClose }: ForkHistoryPanelProps) {
  return (
    <aside
      className="absolute right-0 top-0 h-full w-80 bg-white border-l border-gray-200 shadow-lg flex flex-col"
      style={{ zIndex: 15 }}
      aria-label="Fork version history panel"
    >
      <div className="flex items-center justify-between px-4 py-3 border-b border-gray-200">
        <h3 className="text-sm font-semibold text-gray-900">Fork version history</h3>
        <button
          onClick={onClose}
          aria-label="Close history panel"
          className="text-gray-400 hover:text-gray-600 text-xl leading-none px-1"
        >
          &times;
        </button>
      </div>

      <div className="flex-1 overflow-y-auto divide-y divide-gray-100">
        {versions.length === 0 && (
          <p className="px-4 py-3 text-sm text-gray-500">
            No fork versions yet. Commit an edit to create the first version.
          </p>
        )}
        {versions.map((v) => (
          <div key={v.id} className="px-4 py-3 text-sm">
            <div className="flex items-center gap-2">
              <span className="font-semibold text-gray-900">v{v.version_number}</span>
              {v.restored_from_id && (
                <span className="text-xs px-1.5 py-0.5 rounded bg-amber-100 text-amber-800">
                  restore
                </span>
              )}
            </div>
            <div className="text-xs text-gray-500">{formatDate(v.created_at)}</div>
          </div>
        ))}
      </div>
    </aside>
  );
}
