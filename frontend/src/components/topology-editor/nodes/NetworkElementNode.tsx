import { useState, useRef, useEffect } from "react";
import { Handle, Position, type NodeProps } from "@xyflow/react";
import { Network, Waypoints, Cloud, Cable } from "lucide-react";
import { useTopologyStore } from "@/stores/topologyStore";
import type {
  NetworkElementNode as NetworkElementNodeType,
  NetworkElementType,
} from "@/types/topology.types";

// Icon per element_type (herd-design: Lucide, outline, currentColor).
const ELEMENT_ICONS: Record<NetworkElementType, typeof Network> = {
  vlan_segment: Network,
  subnet: Waypoints,
  external_cloud: Cloud,
  patch_trunk: Cable,
};

const ELEMENT_LABELS: Record<NetworkElementType, string> = {
  vlan_segment: "VLAN segment",
  subnet: "Subnet",
  external_cloud: "External cloud",
  patch_trunk: "Patch trunk",
};

// Dashed NEUTRAL gray, deliberately distinct from DynamicPlaceholderNode's
// dashed purple (ADR 0012 "Canvas shape"): the two ephemeral-looking node
// kinds must not be confusable, since one persists (this) and one does not
// (the placeholder). Gray is also the herd-design neutral, reserving color
// (blue/purple) for what it already means elsewhere on the canvas.
export function NetworkElementNode({ id, data, selected }: NodeProps<NetworkElementNodeType>) {
  const { element, isProposal } = data;
  const setNetworkElementLabel = useTopologyStore((s) => s.setNetworkElementLabel);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(element.label);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (editing) inputRef.current?.focus();
  }, [editing]);

  const Icon = ELEMENT_ICONS[element.element_type];

  const commit = () => {
    const trimmed = draft.trim();
    setNetworkElementLabel(id, trimmed.length > 0 ? trimmed : element.label);
    setEditing(false);
  };

  const cancel = () => {
    setDraft(element.label);
    setEditing(false);
  };

  return (
    <div
      className={`
        relative rounded-lg border-2 border-dashed border-gray-400 bg-gray-100
        p-3 min-w-[140px] shadow-sm cursor-grab text-gray-700
        ${selected ? "ring-2 ring-offset-1 ring-yellow-400" : ""}
        ${isProposal ? "opacity-70" : ""}
      `}
    >
      {isProposal && (
        <span className="absolute -top-2 -right-2 text-[10px] font-bold px-1.5 py-0.5 rounded bg-purple-600 text-white shadow">
          PROPOSED
        </span>
      )}

      {/* Elements have no ports: exactly one target handle, no source handles. */}
      <Handle type="target" id="element" position={Position.Top} className="!bg-gray-500" />

      <div className="flex flex-col items-center gap-1">
        <span className="inline-flex items-center justify-center w-8 h-8 rounded bg-gray-200 text-gray-600">
          <Icon className="w-4 h-4" />
        </span>
        <span className="text-[10px] uppercase tracking-wide text-gray-500">
          {ELEMENT_LABELS[element.element_type]}
        </span>

        {editing ? (
          <input
            ref={inputRef}
            type="text"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onBlur={commit}
            onKeyDown={(e) => {
              if (e.key === "Enter") commit();
              if (e.key === "Escape") cancel();
            }}
            aria-label={`Label for ${ELEMENT_LABELS[element.element_type]}`}
            className="nodrag w-full text-sm font-semibold text-center border border-gray-300 rounded px-1 py-0.5 bg-white text-gray-900 focus:outline-none focus:ring-2 focus:ring-blue-500"
          />
        ) : (
          <button
            type="button"
            onDoubleClick={() => {
              setDraft(element.label);
              setEditing(true);
            }}
            aria-label={`Edit label for ${element.label}`}
            title="Double-click to rename"
            className="nodrag text-sm font-semibold text-center leading-tight bg-transparent border-none p-0 cursor-text hover:underline"
          >
            {element.label}
          </button>
        )}
      </div>
    </div>
  );
}
