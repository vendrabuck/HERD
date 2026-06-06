interface AIProposalBarProps {
  purpose: string;
  deviceCount: number;
  edgeCount: number;
  notes?: string;
  onAccept: () => void;
  onModify: () => void;
  onReject: () => void;
}

export function AIProposalBar({
  purpose,
  deviceCount,
  edgeCount,
  notes,
  onAccept,
  onModify,
  onReject,
}: AIProposalBarProps) {
  return (
    <div className="absolute top-4 left-1/2 -translate-x-1/2 z-10 bg-white border-2 border-purple-400 rounded-lg shadow-lg px-4 py-3 max-w-2xl">
      <div className="flex items-start gap-4">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <span className="text-xs font-bold px-1.5 py-0.5 rounded bg-purple-600 text-white">
              AI PROPOSAL
            </span>
            <span className="text-sm font-medium text-gray-900 truncate">
              {purpose || "Untitled proposal"}
            </span>
          </div>
          <p className="text-xs text-gray-600 mt-1">
            {deviceCount} device{deviceCount !== 1 ? "s" : ""}, {edgeCount} edge
            {edgeCount !== 1 ? "s" : ""}. Review before committing.
          </p>
          {notes && <p className="text-xs text-gray-500 mt-1 italic">{notes}</p>}
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <button
            onClick={onReject}
            className="text-sm px-3 py-1 rounded text-red-700 hover:bg-red-50 border border-red-300"
          >
            Reject
          </button>
          <button
            onClick={onModify}
            className="text-sm px-3 py-1 rounded text-gray-700 hover:bg-gray-100 border border-gray-300"
          >
            Modify
          </button>
          <button
            onClick={onAccept}
            className="text-sm px-3 py-1 rounded text-white bg-purple-600 hover:bg-purple-700"
          >
            Accept
          </button>
        </div>
      </div>
    </div>
  );
}
