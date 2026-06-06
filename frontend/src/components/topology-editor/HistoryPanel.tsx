import { useMemo, useState } from "react";

import { useTopologyVersions } from "@/api/topologies";
import type { TopologyVersion } from "@/types/topology.types";

interface HistoryPanelProps {
  topologyId: string;
  onClose: () => void;
  onPreview: (version: TopologyVersion) => void;
  onRestore: (version: TopologyVersion) => void;
  onCompare: (a: TopologyVersion, b: TopologyVersion) => void;
  previewVersionId?: string | null;
}

function formatDate(iso: string): string {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

export function HistoryPanel({
  topologyId,
  onClose,
  onPreview,
  onRestore,
  onCompare,
  previewVersionId,
}: HistoryPanelProps) {
  const [selected, setSelected] = useState<string[]>([]);
  const { data, isLoading, isError } = useTopologyVersions(topologyId, 0, 200);

  const versionsById = useMemo(() => {
    const map = new Map<string, TopologyVersion>();
    (data?.items ?? []).forEach((v) => map.set(v.id, v));
    return map;
  }, [data]);

  const toggleSelect = (id: string) => {
    setSelected((prev) => {
      if (prev.includes(id)) return prev.filter((x) => x !== id);
      if (prev.length >= 2) return [prev[1], id];
      return [...prev, id];
    });
  };

  const handleCompare = () => {
    if (selected.length !== 2) return;
    const a = versionsById.get(selected[0]);
    const b = versionsById.get(selected[1]);
    if (a && b) onCompare(a, b);
  };

  return (
    <aside
      className="absolute right-0 top-0 h-full w-80 bg-white border-l border-gray-200 shadow-lg flex flex-col"
      style={{ zIndex: 15 }}
      aria-label="Version history panel"
    >
      <div className="flex items-center justify-between px-4 py-3 border-b border-gray-200">
        <h3 className="text-sm font-semibold text-gray-900">Version history</h3>
        <button
          onClick={onClose}
          aria-label="Close history panel"
          className="text-gray-400 hover:text-gray-600 text-xl leading-none px-1"
        >
          &times;
        </button>
      </div>

      <div className="px-4 py-2 border-b border-gray-200 flex items-center gap-2 text-xs">
        <span className="text-gray-500">{selected.length}/2 selected</span>
        <button
          onClick={handleCompare}
          disabled={selected.length !== 2}
          className="ml-auto px-2 py-1 rounded bg-blue-600 text-white disabled:bg-gray-300 disabled:cursor-not-allowed hover:bg-blue-700"
        >
          Compare
        </button>
      </div>

      <div className="flex-1 overflow-y-auto divide-y divide-gray-100">
        {isLoading && (
          <p className="px-4 py-3 text-sm text-gray-500">Loading versions...</p>
        )}
        {isError && (
          <p className="px-4 py-3 text-sm text-red-600">Failed to load versions.</p>
        )}
        {data && data.items.length === 0 && (
          <p className="px-4 py-3 text-sm text-gray-500">
            No versions yet. Save the topology to create the first snapshot.
          </p>
        )}
        {data?.items.map((v) => {
          const isPreview = v.id === previewVersionId;
          const isSelected = selected.includes(v.id);
          return (
            <div
              key={v.id}
              className={`px-4 py-3 text-sm ${isPreview ? "bg-purple-50" : ""}`}
            >
              <div className="flex items-start gap-2">
                <input
                  type="checkbox"
                  checked={isSelected}
                  onChange={() => toggleSelect(v.id)}
                  className="mt-1"
                  aria-label={`Select version ${v.version_number}`}
                />
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="font-semibold text-gray-900">
                      v{v.version_number}
                    </span>
                    <span className="text-xs text-gray-500">
                      by {v.author_name || "unknown"}
                    </span>
                    {v.restored_from_id && (
                      <span className="text-xs px-1.5 py-0.5 rounded bg-amber-100 text-amber-800">
                        restore
                      </span>
                    )}
                  </div>
                  <div className="text-xs text-gray-500">{formatDate(v.created_at)}</div>
                  {v.description && (
                    <div className="text-xs text-gray-700 mt-1 break-words">
                      {v.description}
                    </div>
                  )}
                  <div className="flex gap-3 mt-2">
                    <button
                      onClick={() => onPreview(v)}
                      className="text-xs text-purple-600 hover:text-purple-800"
                    >
                      {isPreview ? "Previewing" : "View"}
                    </button>
                    <button
                      onClick={() => onRestore(v)}
                      className="text-xs text-amber-600 hover:text-amber-800"
                    >
                      Restore
                    </button>
                  </div>
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </aside>
  );
}
