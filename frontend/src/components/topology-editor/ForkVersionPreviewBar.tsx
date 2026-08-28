import type { ForkVersionSummary } from "@/types/reservation.types";

interface ForkVersionPreviewBarProps {
  mode: "preview" | "diff";
  previewVersion: ForkVersionSummary | null;
  diffBase: ForkVersionSummary | null;
  diffCompareLabel: string | null;
  onExit: () => void;
}

// Shown while ForkHistoryPanel's Preview or Diff action has taken over the
// canvas (issue #622, ADR 0006 addendum). Both are read-only: editing, the
// wiring dialog, and Save are all disabled while this bar is up (gated
// through TopologyEditorPage's isReadOnly, driven by useForkVersionPreview's
// isActive), and exiting restores the live draft exactly as it was.
export function ForkVersionPreviewBar({
  mode,
  previewVersion,
  diffBase,
  diffCompareLabel,
  onExit,
}: ForkVersionPreviewBarProps) {
  const isDiff = mode === "diff";
  const title = isDiff
    ? `Comparing v${diffBase?.version_number ?? "?"} to ${diffCompareLabel ?? "..."}`
    : `Previewing version ${previewVersion?.version_number ?? "?"}`;
  const exitLabel = isDiff
    ? "Exit diff"
    : `Exit preview (v${previewVersion?.version_number ?? "?"})`;

  return (
    <div
      className="absolute top-4 left-1/2 -translate-x-1/2 z-10 bg-white border-2 border-purple-400 rounded-lg shadow-lg px-4 py-3 max-w-2xl"
      role="status"
    >
      <div className="flex items-start gap-4">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <span className="text-xs font-bold px-1.5 py-0.5 rounded bg-purple-600 text-white">
              {isDiff ? "COMPARING VERSIONS" : "HISTORY PREVIEW"}
            </span>
            <span className="text-sm font-medium text-gray-900">{title}</span>
          </div>
          <p className="text-xs text-gray-600 mt-1">
            Read-only: editing, wiring, and Save are disabled until you exit.
          </p>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <button
            onClick={onExit}
            className="text-sm px-3 py-1 rounded text-purple-700 bg-purple-100 hover:bg-purple-200"
          >
            {exitLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
