import { useState } from "react";

import type { ForkConnectionDelta, ForkSaveResult } from "@/types/reservation.types";

interface ForkSaveResultToastProps {
  result: ForkSaveResult;
  onDismiss: () => void;
}

function shortId(id: string): string {
  return id.length > 8 ? id.slice(0, 8) : id;
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
// per-connection release and build lists.
export function ForkSaveResultToast({ result, onDismiss }: ForkSaveResultToastProps) {
  const [expanded, setExpanded] = useState(false);
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
