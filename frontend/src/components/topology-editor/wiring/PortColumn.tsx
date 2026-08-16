import { memo, useCallback, useMemo } from "react";
import { List, type RowComponentProps } from "react-window";
import { Search } from "lucide-react";
import { TopoBadge } from "@/components/ui/TopoBadge";
import { cn } from "@/lib/cn";
import type { Port } from "@/types/port.types";
import type { EdgeLayerType } from "@/types/topology.types";
import type { TopologyType } from "@/types/device.types";
import { LAYER_STYLES } from "../edges/layerStyles";

export type Side = "source" | "target";

// "cabled" is informational only (issue #517 review item 1): a port with a
// registered physical connection is still fully selectable, it just carries
// a small tag. "session-wired" (used earlier in THIS dialog session) and
// "canvas-wired" (already connected to the counterpart by an existing
// canvas edge, review item 8) both block interaction and render the same
// WIRED tag; only the tooltip/error text distinguishes them.
export type PortStatus = "free" | "session-wired" | "canvas-wired";

export interface PortRowVisual {
  status: PortStatus;
  layer?: EdgeLayerType;
  cabled: boolean;
}

export const ROW_HEIGHT = 28;
const LIST_HEIGHT = 260;

interface RowData {
  ports: Port[];
  side: Side;
  visuals: Map<string, PortRowVisual>;
  armedPortId: string | null;
  highlightFree: boolean;
  interactionDisabled: boolean;
  onPortMouseDown: (port: Port, e: React.MouseEvent) => void;
  onPortActivate: (port: Port) => void;
  registerRowRef: (portId: string, el: HTMLDivElement | null) => void;
}

function PortRowImpl({
  index,
  style,
  ports,
  side,
  visuals,
  armedPortId,
  highlightFree,
  interactionDisabled,
  onPortMouseDown,
  onPortActivate,
  registerRowRef,
}: RowComponentProps<RowData>) {
  const port = ports[index];
  const visual = visuals.get(port.id) ?? { status: "free" as const, cabled: false };
  const isArmed = armedPortId === port.id;
  const isFree = visual.status === "free" && !interactionDisabled;
  const isHighlighted = isFree && highlightFree;

  let dotStyle: React.CSSProperties = {
    background: "white",
    borderColor: "#9ca3af",
  };
  if (visual.status === "session-wired" || visual.status === "canvas-wired") {
    const fill = LAYER_STYLES[visual.layer ?? "L2"].stroke;
    dotStyle = { background: fill, borderColor: fill };
  } else if (isHighlighted) {
    dotStyle = { background: "white", borderColor: "#3b82f6" };
  }

  const rowClassName = cn(
    "flex items-center h-full px-2 text-xs border-b border-gray-100 select-none",
    isFree ? "cursor-pointer hover:bg-gray-50" : "cursor-not-allowed text-gray-400",
    isArmed && "bg-blue-50 ring-1 ring-inset ring-blue-500",
    isHighlighted && "bg-[#f0f6ff]",
    side === "target" && "flex-row-reverse text-right",
  );

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (!isFree) return;
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      onPortActivate(port);
    }
  };

  // A stable ref callback keyed on port.id (issue #517 review round 3 item
  // 8): an inline `ref={(el) => registerRowRef(port.id, el)}` is a NEW
  // function every render, which makes React detach-then-reattach the ref
  // (calling the old one with null, then the new one with the element) even
  // when the underlying DOM node is reused across a virtualization re-render
  // with an unchanged port.id, a needless churn of the row-ref map.
  const setRowRef = useCallback(
    (el: HTMLDivElement | null) => registerRowRef(port.id, el),
    [registerRowRef, port.id],
  );

  return (
    <div
      style={style}
      className={rowClassName}
      data-port-id={port.id}
      data-side={side}
      data-status={visual.status}
      data-testid={`port-row-${port.id}`}
      ref={setRowRef}
      tabIndex={isFree ? 0 : -1}
      role="button"
      aria-pressed={isArmed}
      aria-disabled={!isFree}
      onMouseDown={(e) => {
        if (interactionDisabled) return;
        onPortMouseDown(port, e);
      }}
      onKeyDown={handleKeyDown}
      title={
        visual.status === "session-wired"
          ? "Already has a line in this session"
          : visual.status === "canvas-wired"
            ? "already connected on the canvas"
            : undefined
      }
    >
      <span
        className="inline-block w-2 h-2 rounded-full border shrink-0"
        style={dotStyle}
        data-testid={`port-dot-${port.id}`}
      />
      <span className={`flex-1 min-w-0 truncate font-mono text-[13px] ${side === "target" ? "mr-2" : "ml-2"}`}>
        {port.name}
      </span>
      {visual.status === "free" && visual.cabled && (
        <span className="text-[10px] uppercase text-gray-400 shrink-0">CABLED</span>
      )}
      {visual.status === "free" && !visual.cabled && (
        <span className="text-[10px] text-gray-300 shrink-0">no cable</span>
      )}
      {(visual.status === "session-wired" || visual.status === "canvas-wired") && (
        <span className="text-[10px] uppercase text-gray-400 shrink-0">WIRED</span>
      )}
    </div>
  );
}

// React.memo only pays off if the row's own props are referentially stable
// across unrelated re-renders; PortColumn memoizes the shared rowProps object
// below so it is (issue #517 review item 10).
const PortRow = memo(PortRowImpl) as typeof PortRowImpl;

export interface PortColumnProps {
  side: Side;
  deviceName: string;
  topologyType: TopologyType;
  // Already filtered (issue #517 review round 3 item 12.2): the caller
  // (WiringDialog) computes filterPorts once per side and passes the result
  // straight through, rather than PortColumn calling filterPorts again on
  // its own copy of the same ports array. totalCount is the UNFILTERED
  // count, needed for the header line and the "No ports configured" vs
  // "No ports match" distinction.
  ports: Port[];
  totalCount: number;
  isLoading: boolean;
  visuals: Map<string, PortRowVisual>;
  cabledCount: number;
  filter: string;
  onFilterChange: (value: string) => void;
  armedPortId: string | null;
  highlightFree: boolean;
  interactionDisabled: boolean;
  onPortMouseDown: (port: Port, e: React.MouseEvent) => void;
  onPortActivate: (port: Port) => void;
  registerRowRef: (portId: string, el: HTMLDivElement | null) => void;
  onScroll: (e: React.UIEvent<HTMLDivElement>) => void;
}

// Memoized (issue #517 review item 10c): scrolling one column bumps only
// that side's scroll tick in WiringDialog, so as long as this column's own
// props stay referentially stable, it should not re-render just because its
// sibling column scrolled.
function PortColumnImpl({
  side,
  deviceName,
  topologyType,
  ports,
  totalCount,
  isLoading,
  visuals,
  cabledCount,
  filter,
  onFilterChange,
  armedPortId,
  highlightFree,
  interactionDisabled,
  onPortMouseDown,
  onPortActivate,
  registerRowRef,
  onScroll,
}: PortColumnProps) {
  const rowKey = useCallback((index: number, data: RowData) => data.ports[index].id, []);

  const rowProps: RowData = useMemo(
    () => ({
      ports,
      side,
      visuals,
      armedPortId,
      highlightFree,
      interactionDisabled,
      onPortMouseDown,
      onPortActivate,
      registerRowRef,
    }),
    [
      ports,
      side,
      visuals,
      armedPortId,
      highlightFree,
      interactionDisabled,
      onPortMouseDown,
      onPortActivate,
      registerRowRef,
    ],
  );

  return (
    <div className="flex flex-col w-[240px] shrink-0 border-gray-200" data-testid={`port-column-${side}`}>
      <div className="h-[94px] shrink-0 bg-gray-50 border-b border-gray-200 px-2 py-1.5 flex flex-col gap-1">
        <div className="flex items-center gap-1 min-w-0">
          <span className="text-sm font-semibold text-gray-900 truncate">{deviceName}</span>
          <TopoBadge type={topologyType} />
        </div>
        <div className="relative">
          <Search className="absolute left-1.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-gray-400" />
          <input
            type="text"
            value={filter}
            onChange={(e) => onFilterChange(e.target.value)}
            placeholder="Filter ports"
            aria-label={`Filter ${side} ports`}
            className="w-full h-[28px] pl-6 pr-2 rounded-md border border-gray-300 text-xs focus:outline-none focus:ring-2 focus:ring-blue-500"
          />
        </div>
        <span className="text-[11px] text-gray-500">
          {totalCount} port{totalCount === 1 ? "" : "s"}, {cabledCount} cabled
        </span>
      </div>
      <div className="flex-1 min-h-0 overflow-hidden">
        {isLoading ? (
          <p className="text-xs text-gray-400 p-2">Loading ports...</p>
        ) : totalCount === 0 ? (
          <p className="text-xs text-gray-400 p-2">No ports configured</p>
        ) : ports.length === 0 ? (
          <p className="text-xs text-gray-400 p-2">No ports match</p>
        ) : (
          <List
            rowComponent={PortRow}
            rowCount={ports.length}
            rowHeight={ROW_HEIGHT}
            rowProps={rowProps}
            rowKey={rowKey}
            // An explicit pixel height (issue #517 review round 3 item 10),
            // not "100%": a percentage height only resolves inside an
            // ancestor chain with a DEFINITE height, and a flex:1 child's
            // own computed height is "auto" unless something further pins
            // it, so react-window's internal measurement could plausibly
            // read 0 in a real browser's flex layout even though jsdom
            // (which never actually lays anything out) cannot show it.
            style={{ height: LIST_HEIGHT }}
            onScroll={onScroll}
          />
        )}
      </div>
    </div>
  );
}

export const PortColumn = memo(PortColumnImpl) as typeof PortColumnImpl;
