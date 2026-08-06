import { Handle, Position, type NodeProps } from "@xyflow/react";
import { useTopologyStore } from "@/stores/topologyStore";
import type { DynamicPlaceholderNode as DynamicPlaceholderNodeType } from "@/types/topology.types";

// Mirrors the backend cap: ReservationCreate.dynamic_requests has max_length=50.
const MAX_DYNAMIC_REQUESTS = 50;

function clampCount(raw: number): number {
  if (Number.isNaN(raw) || raw < 1) return 1;
  if (raw > MAX_DYNAMIC_REQUESTS) return MAX_DYNAMIC_REQUESTS;
  return Math.floor(raw);
}

// Canvas placeholder for a dynamic (hypervisor-backed) template. Dashed border
// and purple accent in the ghost-node spirit: it stands for instances that do
// not exist yet. It carries no inventory device id, takes no cabling, and is
// never persisted; reserving expands its count into dynamic_requests.
export function DynamicPlaceholderNode({
  id,
  data,
  selected,
}: NodeProps<DynamicPlaceholderNodeType>) {
  const setDynamicPlaceholderCount = useTopologyStore((s) => s.setDynamicPlaceholderCount);

  return (
    <div
      className={`
        relative rounded-lg border-2 border-dashed border-purple-400 bg-purple-50
        p-3 min-w-[140px] shadow-sm cursor-grab text-purple-900
        ${selected ? "ring-2 ring-offset-1 ring-yellow-400" : ""}
      `}
    >
      <span className="absolute -top-2 -right-2 text-[10px] font-bold px-1.5 py-0.5 rounded bg-purple-600 text-white shadow">
        DYNAMIC
      </span>
      <Handle type="source" id="top" position={Position.Top} className="!bg-gray-500" />
      <Handle type="source" id="right" position={Position.Right} className="!bg-gray-500" />

      <div className="flex flex-col items-center gap-1">
        {data.templateIcon ? (
          <img src={data.templateIcon} alt={data.templateName} className="w-8 h-8 object-contain" />
        ) : (
          <span className="inline-block w-8 h-8 bg-gray-300 rounded" />
        )}
        <span className="text-sm font-semibold text-center leading-tight">{data.templateName}</span>
        <label className="flex items-center gap-1 text-xs text-purple-800">
          Instances
          <input
            type="number"
            min={1}
            max={MAX_DYNAMIC_REQUESTS}
            value={data.count}
            aria-label={`Instance count for ${data.templateName}`}
            onChange={(e) => setDynamicPlaceholderCount(id, clampCount(e.target.valueAsNumber))}
            className="nodrag w-14 border border-purple-300 rounded px-1 py-0.5 text-xs bg-white text-gray-900 focus:outline-none focus:ring-2 focus:ring-blue-500"
          />
        </label>
        <span className="text-xs text-gray-500">Created at activation</span>
      </div>

      <Handle type="source" id="bottom" position={Position.Bottom} className="!bg-gray-500" />
      <Handle type="source" id="left" position={Position.Left} className="!bg-gray-500" />
    </div>
  );
}
