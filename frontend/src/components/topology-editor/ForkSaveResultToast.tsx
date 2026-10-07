import { useState } from "react";

import { shortId, skippedEdgeText } from "@/lib/forkSaveResult";
import type { ForkConnectionDelta, ForkSaveResult } from "@/types/reservation.types";

interface ForkSaveResultToastProps {
  result: ForkSaveResult;
  onDismiss: () => void;
  // Device id to display name, from the canvas that was just saved. A device
  // missing from the map falls back to its short id.
  deviceLabels?: Record<string, string>;
}

function DeltaRow({ delta, tone }: { delta: ForkConnectionDelta; tone: "release" | "build" }) {
  const badge =
    tone === "release" ? "bg-amber-100 text-amber-800" : "bg-green-100 text-green-800";
  return (
    <li className="flex items-center gap-2 py-0.5">
      <span className={`text-[10px] font-semibold px-1 rounded ${badge}`}>{delta.layer}</span>
      <span className="font-mono text-[11px] text-gray-700 break-all">
        {shortId(delta.device_a_id)}/{delta.port_a} to {shortId(delta.device_b_id)}/{delta.port_b}
      </span>
    </li>
  );
}

// Custom react-hot-toast body for a successful fork reconcile (ADR 0006
// Decision 6). Shows the released/built/unchanged counts, expandable to the
// per-connection release and build lists. Lines the save could not wire on
// their chosen ports (issue #1007) are always listed, never behind the toggle.
export function ForkSaveResultToast({ result, onDismiss, deviceLabels }: ForkSaveResultToastProps) {
  const [expanded, setExpanded] = useState(false);
  const skipped = result.constrained_edges_skipped ?? [];
  const hasDetail = result.released.length > 0 || result.built.length > 0;

  return (
    <div className="bg-white border border-gray-200 rounded-lg shadow-lg px-4 py-3 max-w-md w-full">
      <div className="flex items-start gap-3">
        <div className="flex-1 min-w-0">
          <p className="text-sm font-semibold text-gray-900">Fork saved as v{result.version_number}</p>
          <p className="text-xs text-gray-600 mt-0.5">
            Released {result.released.length}, built {result.built.length}, unchanged{" "}
            {result.unchanged_count}
          </p>
          {!!result.element_attachments_skipped && result.element_attachments_skipped > 0 && (
            <p className="text-xs text-gray-600 mt-0.5">
              {result.element_attachments_skipped} element attachment
              {result.element_attachments_skipped === 1 ? "" : "s"} recorded (not wired)
            </p>
          )}
          {skipped.length > 0 && (
            <div className="mt-1" role="alert">
              <p className="text-xs font-semibold text-amber-800">
                {skipped.length} line{skipped.length === 1 ? "" : "s"} not wired: no cable path on
                the chosen ports
              </p>
              <ul>
                {skipped.map((edge, i) => (
                  <li
                    key={edge.edge_id ?? `skipped-${i}`}
                    className="text-[11px] font-mono text-amber-800 break-all"
                  >
                    {skippedEdgeText(edge, deviceLabels)}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {hasDetail && (
            <button
              onClick={() => setExpanded((v) => !v)}
              className="text-xs text-blue-600 hover:text-blue-800 mt-1"
            >
              {expanded ? "Hide detail" : "Show detail"}
            </button>
          )}
          {expanded && (
            <div className="mt-2 space-y-2">
              {result.released.length > 0 && (
                <div>
                  <p className="text-[11px] font-semibold uppercase text-amber-700">Released</p>
                  <ul>
                    {result.released.map((d, i) => (
                      <DeltaRow key={`rel-${i}`} delta={d} tone="release" />
                    ))}
                  </ul>
                </div>
              )}
              {result.built.length > 0 && (
                <div>
                  <p className="text-[11px] font-semibold uppercase text-green-700">Built</p>
                  <ul>
                    {result.built.map((d, i) => (
                      <DeltaRow key={`build-${i}`} delta={d} tone="build" />
                    ))}
                  </ul>
                </div>
              )}
            </div>
          )}
        </div>
        <button
          onClick={onDismiss}
          aria-label="Dismiss"
          className="text-gray-400 hover:text-gray-600 text-lg leading-none"
        >
          &times;
        </button>
      </div>
    </div>
  );
}
