import { useState } from "react";
import toast from "react-hot-toast";

import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import type { ForkVersionSummary } from "@/types/reservation.types";
import type { DeviceNodeData } from "@/types/topology.types";
import type { ForkDiffCompareTarget, UseForkVersionPreviewResult } from "@/hooks/useForkVersionPreview";

interface ForkHistoryPanelProps {
  versions: ForkVersionSummary[];
  // Restore mirrors the Retry button's ACTIVE-only rule (ADR 0009): it is
  // rendered only for an ACTIVE reservation, never merely enabled/disabled,
  // so a non-ACTIVE fork's history reads as pure record with no dangling
  // control.
  isActiveReservation: boolean;
  // ReservationFork.draft_restored_from_id (contract revised 2026-08-28):
  // non-null only while the draft holds a restored-but-unsaved snapshot.
  // Restore itself appends no fork_versions row (the "restored" marker shows
  // up on the version the NEXT Save creates instead), so this is the only
  // signal that the draft is a pending restore.
  draftRestoredFromId: string | null;
  preview: UseForkVersionPreviewResult;
  onClose: () => void;
}

function formatDate(iso: string): string {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

function nodeLabel(node: { id: string; data?: unknown }): string {
  const data = node.data as DeviceNodeData | undefined;
  return data?.label || data?.device?.name || node.id;
}

function edgeLabel(edge: { data?: unknown }): string {
  const data = edge.data as { layer?: string; source_port_name?: string; target_port_name?: string } | undefined;
  const ports =
    data?.source_port_name && data?.target_port_name
      ? `${data.source_port_name} - ${data.target_port_name}`
      : (data?.source_port_name ?? data?.target_port_name ?? "unnamed ports");
  return `${data?.layer ?? "?"}: ${ports}`;
}

// The version history for a reservation fork (ADR 0006 Decision 6, ADR 0006
// addendum / issue #622). Each fork save appends a version, listed newest-
// first. Every row can Preview (read-only render on the canvas), Diff
// (against another version or the current draft, with the added/removed
// lists rendered below), and, for an ACTIVE reservation, Restore (copies the
// version's canvas back onto the draft; nothing is wired until Save). Restore
// appends no version of its own (contract revised 2026-08-28): while the
// draft holds an unsaved restore, an amber chip reads "Draft restored from
// version N (unsaved)" from draftRestoredFromId; the "restored" marker below
// only appears once the user's next Save creates the version that carries
// restored_from_id.
export function ForkHistoryPanel({
  versions,
  isActiveReservation,
  draftRestoredFromId,
  preview,
  onClose,
}: ForkHistoryPanelProps) {
  const [diffPickerFor, setDiffPickerFor] = useState<string | null>(null);
  const [diffTarget, setDiffTarget] = useState<string>("current");
  const [restoreCandidate, setRestoreCandidate] = useState<ForkVersionSummary | null>(null);

  const draftRestoredFromVersion = draftRestoredFromId
    ? (versions.find((v) => v.id === draftRestoredFromId) ?? null)
    : null;

  const openDiffPicker = (version: ForkVersionSummary) => {
    setDiffPickerFor(version.id);
    setDiffTarget("current");
  };

  const confirmDiff = (base: ForkVersionSummary) => {
    let target: ForkDiffCompareTarget;
    if (diffTarget === "current") {
      target = { kind: "current" };
    } else {
      const version = versions.find((v) => v.id === diffTarget);
      if (!version) {
        // The picked version fell out of the list between opening the picker
        // and clicking Compare (e.g. the panel re-rendered with a fresh
        // versions prop). Bail out rather than throwing on the old `!`
        // non-null assertion.
        setDiffPickerFor(null);
        toast.error("That version is no longer available");
        return;
      }
      target = { kind: "version", version };
    }
    preview.startDiff(base, target);
    setDiffPickerFor(null);
  };

  const handleRestoreConfirm = () => {
    if (!restoreCandidate) return;
    const version = restoreCandidate;
    setRestoreCandidate(null);
    void preview.restoreVersion(version);
  };

  return (
    <aside
      className="absolute right-0 top-0 h-full w-96 bg-white border-l border-gray-200 shadow-lg flex flex-col"
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

      {draftRestoredFromId && (
        <div className="px-4 py-2 bg-amber-50 border-b border-amber-200 text-xs text-amber-800">
          Draft restored from version {draftRestoredFromVersion?.version_number ?? "?"} (unsaved)
        </div>
      )}

      <div className="flex-1 overflow-y-auto divide-y divide-gray-100">
        {versions.length === 0 && (
          <p className="px-4 py-3 text-sm text-gray-500">
            No fork versions yet. Commit an edit to create the first version.
          </p>
        )}
        {versions.map((v) => {
          const isPreviewingThis = preview.mode === "preview" && preview.previewVersion?.id === v.id;
          const isDiffBaseThis = preview.mode === "diff" && preview.diffBase?.id === v.id;
          return (
            <div
              key={v.id}
              className={`px-4 py-3 text-sm ${isPreviewingThis || isDiffBaseThis ? "bg-purple-50" : ""}`}
            >
              <div className="flex items-center gap-2">
                <span className="font-semibold text-gray-900">v{v.version_number}</span>
                {v.restored_from_id && (
                  <span className="text-xs px-1.5 py-0.5 rounded bg-amber-100 text-amber-800">
                    restore
                  </span>
                )}
              </div>
              <div className="text-xs text-gray-500">{formatDate(v.created_at)}</div>

              <div className="flex gap-3 mt-2 flex-wrap">
                <button
                  onClick={() => preview.startPreview(v)}
                  className="text-xs text-purple-600 hover:text-purple-800"
                >
                  {isPreviewingThis ? "Previewing" : "Preview"}
                </button>
                <button
                  onClick={() => openDiffPicker(v)}
                  className="text-xs text-blue-600 hover:text-blue-800"
                >
                  Diff
                </button>
                {isActiveReservation && (
                  <button
                    onClick={() => setRestoreCandidate(v)}
                    disabled={preview.isRestoring}
                    className="text-xs text-amber-600 hover:text-amber-800 disabled:opacity-50"
                  >
                    Restore
                  </button>
                )}
              </div>

              {diffPickerFor === v.id && (
                <div className="mt-2 flex items-center gap-2 text-xs">
                  <select
                    aria-label={`Diff v${v.version_number} against`}
                    value={diffTarget}
                    onChange={(e) => setDiffTarget(e.target.value)}
                    className="border border-gray-300 rounded px-1 py-0.5"
                  >
                    <option value="current">current draft</option>
                    {versions
                      .filter((other) => other.id !== v.id)
                      .map((other) => (
                        <option key={other.id} value={other.id}>
                          v{other.version_number}
                        </option>
                      ))}
                  </select>
                  <button
                    onClick={() => confirmDiff(v)}
                    className="text-blue-600 hover:text-blue-800 font-medium"
                  >
                    Compare
                  </button>
                  <button
                    onClick={() => setDiffPickerFor(null)}
                    className="text-gray-500 hover:text-gray-700"
                  >
                    Cancel
                  </button>
                </div>
              )}
            </div>
          );
        })}
      </div>

      {preview.mode === "diff" && (
        <div className="border-t border-gray-200 px-4 py-3 text-xs max-h-64 overflow-y-auto">
          <div className="flex items-center justify-between mb-2">
            <span className="font-semibold text-gray-900">
              Diff: v{preview.diffBase?.version_number ?? "?"} to {preview.diffCompareLabel ?? "..."}
            </span>
            <button onClick={preview.exit} className="text-gray-500 hover:text-gray-700">
              Exit diff
            </button>
          </div>
          {preview.diffLoading && <p className="text-gray-500">Loading diff...</p>}
          {!preview.diffLoading && preview.diffResult && (
            <div className="space-y-2">
              <DiffList
                title="Added devices"
                items={preview.diffResult.addedNodes.map(nodeLabel)}
                color="text-green-700"
              />
              <DiffList
                title="Removed devices"
                items={preview.diffResult.removedNodes.map(nodeLabel)}
                color="text-red-700"
              />
              <DiffList
                title="Added connections"
                items={preview.diffResult.addedEdges.map(edgeLabel)}
                color="text-green-700"
              />
              <DiffList
                title="Removed connections"
                items={preview.diffResult.removedEdges.map(edgeLabel)}
                color="text-red-700"
              />
              {preview.diffResult.addedNodes.length === 0 &&
                preview.diffResult.removedNodes.length === 0 &&
                preview.diffResult.addedEdges.length === 0 &&
                preview.diffResult.removedEdges.length === 0 && (
                  <p className="text-gray-500">No differences.</p>
                )}
            </div>
          )}
        </div>
      )}

      <ConfirmDialog
        open={!!restoreCandidate}
        title={`Restore v${restoreCandidate?.version_number ?? "?"}?`}
        description="This replaces the fork's draft canvas with this version's snapshot. Nothing is wired until you run Save."
        confirmLabel="Restore"
        onConfirm={handleRestoreConfirm}
        onCancel={() => setRestoreCandidate(null)}
      />
    </aside>
  );
}

function DiffList({ title, items, color }: { title: string; items: string[]; color: string }) {
  if (items.length === 0) return null;
  return (
    <div>
      <p className={`font-medium ${color}`}>
        {title} ({items.length})
      </p>
      <ul className="list-disc pl-4 text-gray-700">
        {items.map((item, i) => (
          <li key={i}>{item}</li>
        ))}
      </ul>
    </div>
  );
}
