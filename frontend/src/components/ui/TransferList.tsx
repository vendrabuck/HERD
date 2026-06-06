import { useState, useMemo } from "react";

export interface TransferItem {
  id: string;
  label: string;
  sublabel?: string;
}

interface TransferListProps {
  availableItems: TransferItem[];
  assignedItems: TransferItem[];
  onAssign: (ids: string[]) => void;
  onUnassign: (ids: string[]) => void;
  availableLabel?: string;
  assignedLabel?: string;
  isLoading?: boolean;
}

export function TransferList({
  availableItems,
  assignedItems,
  onAssign,
  onUnassign,
  availableLabel = "Available",
  assignedLabel = "Assigned",
  isLoading = false,
}: TransferListProps) {
  const [leftSelected, setLeftSelected] = useState<Set<string>>(new Set());
  const [rightSelected, setRightSelected] = useState<Set<string>>(new Set());
  const [leftSearch, setLeftSearch] = useState("");
  const [rightSearch, setRightSearch] = useState("");

  const filteredAvailable = useMemo(() => {
    const q = leftSearch.toLowerCase();
    return [...availableItems]
      .filter(
        (item) =>
          !q ||
          item.label.toLowerCase().includes(q) ||
          (item.sublabel && item.sublabel.toLowerCase().includes(q))
      )
      .sort((a, b) => a.label.localeCompare(b.label));
  }, [availableItems, leftSearch]);

  const filteredAssigned = useMemo(() => {
    const q = rightSearch.toLowerCase();
    return [...assignedItems]
      .filter(
        (item) =>
          !q ||
          item.label.toLowerCase().includes(q) ||
          (item.sublabel && item.sublabel.toLowerCase().includes(q))
      )
      .sort((a, b) => a.label.localeCompare(b.label));
  }, [assignedItems, rightSearch]);

  const toggleLeft = (id: string) => {
    setLeftSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const toggleRight = (id: string) => {
    setRightSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const handleAssign = () => {
    if (leftSelected.size === 0) return;
    onAssign([...leftSelected]);
    setLeftSelected(new Set());
  };

  const handleUnassign = () => {
    if (rightSelected.size === 0) return;
    onUnassign([...rightSelected]);
    setRightSelected(new Set());
  };

  if (isLoading) {
    return <p className="text-sm text-gray-500 py-4">Loading...</p>;
  }

  return (
    <div className="flex gap-3 items-stretch">
      {/* Left pane: Available */}
      <div className="flex-1 border border-gray-200 rounded-lg overflow-hidden flex flex-col">
        <div className="bg-gray-50 px-3 py-2 border-b border-gray-200">
          <div className="flex items-center justify-between mb-1">
            <span className="text-xs font-semibold text-gray-600 uppercase">{availableLabel}</span>
            <span className="text-xs text-gray-400">{filteredAvailable.length}</span>
          </div>
          <input
            type="text"
            value={leftSearch}
            onChange={(e) => setLeftSearch(e.target.value)}
            placeholder="Search..."
            className="w-full px-2 py-1 text-xs border border-gray-300 rounded focus:ring-1 focus:ring-blue-500 focus:border-blue-500"
          />
        </div>
        <div className="flex-1 overflow-y-auto max-h-64">
          {filteredAvailable.length === 0 ? (
            <p className="text-xs text-gray-400 px-3 py-3">No items</p>
          ) : (
            filteredAvailable.map((item) => (
              <label
                key={item.id}
                className="flex items-center gap-2 px-3 py-1.5 hover:bg-gray-50 cursor-pointer text-sm"
              >
                <input
                  type="checkbox"
                  checked={leftSelected.has(item.id)}
                  onChange={() => toggleLeft(item.id)}
                  className="rounded border-gray-300"
                />
                <span className="truncate">
                  <span className="text-gray-900">{item.label}</span>
                  {item.sublabel && (
                    <span className="text-gray-400 ml-1 text-xs">{item.sublabel}</span>
                  )}
                </span>
              </label>
            ))
          )}
        </div>
      </div>

      {/* Center buttons */}
      <div className="flex flex-col justify-center gap-2">
        <button
          onClick={handleAssign}
          disabled={leftSelected.size === 0}
          className="px-2 py-1 text-sm border border-gray-300 rounded hover:bg-gray-100 disabled:opacity-30 disabled:cursor-not-allowed"
          title={`Move ${leftSelected.size} to ${assignedLabel.toLowerCase()}`}
        >
          &rarr;
        </button>
        <button
          onClick={handleUnassign}
          disabled={rightSelected.size === 0}
          className="px-2 py-1 text-sm border border-gray-300 rounded hover:bg-gray-100 disabled:opacity-30 disabled:cursor-not-allowed"
          title={`Move ${rightSelected.size} to ${availableLabel.toLowerCase()}`}
        >
          &larr;
        </button>
      </div>

      {/* Right pane: Assigned */}
      <div className="flex-1 border border-gray-200 rounded-lg overflow-hidden flex flex-col">
        <div className="bg-gray-50 px-3 py-2 border-b border-gray-200">
          <div className="flex items-center justify-between mb-1">
            <span className="text-xs font-semibold text-gray-600 uppercase">{assignedLabel}</span>
            <span className="text-xs text-gray-400">{filteredAssigned.length}</span>
          </div>
          <input
            type="text"
            value={rightSearch}
            onChange={(e) => setRightSearch(e.target.value)}
            placeholder="Search..."
            className="w-full px-2 py-1 text-xs border border-gray-300 rounded focus:ring-1 focus:ring-blue-500 focus:border-blue-500"
          />
        </div>
        <div className="flex-1 overflow-y-auto max-h-64">
          {filteredAssigned.length === 0 ? (
            <p className="text-xs text-gray-400 px-3 py-3">No items</p>
          ) : (
            filteredAssigned.map((item) => (
              <label
                key={item.id}
                className="flex items-center gap-2 px-3 py-1.5 hover:bg-gray-50 cursor-pointer text-sm"
              >
                <input
                  type="checkbox"
                  checked={rightSelected.has(item.id)}
                  onChange={() => toggleRight(item.id)}
                  className="rounded border-gray-300"
                />
                <span className="truncate">
                  <span className="text-gray-900">{item.label}</span>
                  {item.sublabel && (
                    <span className="text-gray-400 ml-1 text-xs">{item.sublabel}</span>
                  )}
                </span>
              </label>
            ))
          )}
        </div>
      </div>
    </div>
  );
}
