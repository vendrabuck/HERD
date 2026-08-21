import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Trash2 } from "lucide-react";
import { Modal } from "@/components/ui/Modal";
import { Button } from "@/components/ui/Button";
import { usePortAvailability } from "./wiring/usePortAvailability";
import { EMPTY_ID_SET } from "./wiring/portAvailability";
import { PortColumn, ROW_HEIGHT, type PortRowVisual, type Side } from "./wiring/PortColumn";
import { filterPorts } from "./wiring/filterPorts";
import { LayerSegmentedControl } from "./wiring/LayerSegmentedControl";
import { LayerBadge } from "./wiring/LayerBadge";
import { LAYER_STYLES } from "./edges/layerStyles";
import { resolveEdgeStroke } from "./edges/edgeStatus";
import { genId } from "@/lib/id";
import type { EdgeLayerType } from "@/types/topology.types";
import type { TopologyType } from "@/types/device.types";
import type { Port } from "@/types/port.types";

export interface SessionConnection {
  id: string;
  sourcePortId: string;
  sourcePortName: string;
  targetPortId: string;
  targetPortName: string;
  layer: EdgeLayerType;
  // True only when both ports carry a registered physical connection (any
  // connection, not necessarily to each other): pathfind, run separately by
  // the editor once the edge lands on the canvas, is the real reachability
  // authority. A free-but-unwired port is still allowed (soft warn) and
  // lands false, matching the existing LayerEdge "uncabled port" treatment.
  portsCabled: boolean;
}

export interface WiringDialogProps {
  open: boolean;
  sourceDeviceId: string;
  sourceDeviceName: string;
  sourceTopologyType: TopologyType;
  targetDeviceId: string;
  targetDeviceName: string;
  targetTopologyType: TopologyType;
  defaultLayer: EdgeLayerType;
  // Ports already wired to the counterpart device by an EXISTING canvas
  // edge (issue #517 review item 8): render WIRED/unavailable exactly like
  // a port used earlier in this session, so the same port pair cannot be
  // drawn twice across dialog sessions or after a reload.
  existingWiredSourcePortIds?: ReadonlySet<string>;
  existingWiredTargetPortIds?: ReadonlySet<string>;
  onConfirm: (connections: SessionConnection[]) => void;
  onCancel: () => void;
}

interface Selection {
  side: Side;
  port: Port;
}

interface PressState {
  side: Side;
  port: Port;
  startX: number;
  startY: number;
  moved: boolean;
}

// Layer dropped (issue #517 review round 3 item 12.3): the layer belongs to
// the SessionConnection, the single source of truth; duplicating it here
// risked the two drifting. Geometry is looked up by id and layer is read
// straight from sessionConnections when rendering.
interface LineGeom {
  id: string;
  y1: number;
  y2: number;
  sourceClamped: boolean;
  targetClamped: boolean;
}

// Pointer movement past this many pixels between mousedown and mouseup turns
// the gesture into a drag; short of that it is a click (issue #517 review
// item 2's gesture rework).
const MOVE_THRESHOLD = 4;

// Issue #531 (layer half, decided as canvas-annotation-only, ADR 0009 option
// C): the per-line layer is recorded on the canvas edge but provisioning
// derives L2 membership and L3 route adjacency from the resolved path hops,
// not from this field. Surfaced as a hover tooltip on both layer controls
// below so the annotation-only behavior is discoverable without another
// permanent line of dialog text.
const LAYER_TOOLTIP =
  "Recorded on the canvas as the intended layer for this line. Provisioning derives L2 VLAN " +
  "membership and L3 routes from the resolved path, so this selection does not change what is " +
  "configured.";

function buildVisuals(
  ports: Port[],
  cabled: Set<string>,
  sessionByPortId: Map<string, EdgeLayerType>,
  canvasWiredIds: ReadonlySet<string>,
): Map<string, PortRowVisual> {
  const map = new Map<string, PortRowVisual>();
  for (const port of ports) {
    const sessionLayer = sessionByPortId.get(port.id);
    if (sessionLayer) {
      map.set(port.id, { status: "session-wired", layer: sessionLayer, cabled: cabled.has(port.name) });
    } else if (canvasWiredIds.has(port.id)) {
      map.set(port.id, { status: "canvas-wired", cabled: cabled.has(port.name) });
    } else {
      map.set(port.id, { status: "free", cabled: cabled.has(port.name) });
    }
  }
  return map;
}

// Shared by completeConnection and the "Connect 1:1 in order" loop (issue
// #517 review round 3 item 12.8): both build the exact same SessionConnection
// shape from a source port, a target port, and the session's current
// default layer.
function buildSessionConnection(
  src: Port,
  tgt: Port,
  layer: EdgeLayerType,
  sourceCabled: Set<string>,
  targetCabled: Set<string>,
): SessionConnection {
  return {
    id: genId(),
    sourcePortId: src.id,
    sourcePortName: src.name,
    targetPortId: tgt.id,
    targetPortName: tgt.name,
    layer,
    portsCabled: sourceCabled.has(src.name) && targetCabled.has(tgt.name),
  };
}

export function WiringDialog({
  open,
  sourceDeviceId,
  sourceDeviceName,
  sourceTopologyType,
  targetDeviceId,
  targetDeviceName,
  targetTopologyType,
  defaultLayer,
  existingWiredSourcePortIds = EMPTY_ID_SET,
  existingWiredTargetPortIds = EMPTY_ID_SET,
  onConfirm,
  onCancel,
}: WiringDialogProps) {
  const { source, target, connectionsLoading } = usePortAvailability(sourceDeviceId, targetDeviceId);
  const deviceName: Record<Side, string> = { source: sourceDeviceName, target: targetDeviceName };
  const topologyType: Record<Side, TopologyType> = {
    source: sourceTopologyType,
    target: targetTopologyType,
  };
  const sideData: Record<Side, typeof source> = useMemo(() => ({ source, target }), [source, target]);

  const [sessionConnections, setSessionConnections] = useState<SessionConnection[]>([]);
  const [pendingSource, setPendingSource] = useState<Selection | null>(null);
  // Reactive mirror of the in-flight press, set only once a real drag is
  // detected (crosses MOVE_THRESHOLD); drives the drop-target highlight and
  // the "release on a highlighted port" hint, distinct from a click-armed
  // pendingSource.
  const [dragFrom, setDragFrom] = useState<Selection | null>(null);
  const [newLineLayer, setNewLineLayer] = useState<EdgeLayerType>(defaultLayer);
  const [filter, setFilterState] = useState<Record<Side, string>>({ source: "", target: "" });
  const [selectedLineId, setSelectedLineId] = useState<string | null>(null);
  const [reviewOpen, setReviewOpen] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [lineGeoms, setLineGeoms] = useState<LineGeom[]>([]);
  // The wiring area's measured width (issue #517 review round 3 item 2): SVG
  // path data must be numeric pixel coordinates. Percentages ("50%", "100%")
  // are not valid path syntax at all (confirmed in a real browser: the path
  // fails to parse, `d` is dropped, nothing paints, getTotalLength is 0);
  // jsdom's DOM parser never validates attribute values so this was
  // invisible to every prior vitest run.
  const [areaWidth, setAreaWidth] = useState(0);
  // Per-side scroll ticks (issue #517 review item 10c): scrolling one column
  // only bumps that side's own counter, so the OTHER column's memoized props
  // (and therefore its memoized PortColumn) do not gratuitously churn on
  // every scroll event of its sibling.
  const [scrollTick, setScrollTick] = useState<Record<Side, number>>({ source: 0, target: 0 });

  const areaRef = useRef<HTMLDivElement>(null);
  const rowRefs = useRef<Record<Side, Map<string, HTMLDivElement>>>({
    source: new Map(),
    target: new Map(),
  });
  const scrollTopRef = useRef<Record<Side, number>>({ source: 0, target: 0 });
  const pressRef = useRef<PressState | null>(null);

  const sourceSessionByPortId = useMemo(() => {
    const map = new Map<string, EdgeLayerType>();
    for (const c of sessionConnections) map.set(c.sourcePortId, c.layer);
    return map;
  }, [sessionConnections]);
  const targetSessionByPortId = useMemo(() => {
    const map = new Map<string, EdgeLayerType>();
    for (const c of sessionConnections) map.set(c.targetPortId, c.layer);
    return map;
  }, [sessionConnections]);

  const sourceVisuals = useMemo(
    () => buildVisuals(source.ports, source.cabled, sourceSessionByPortId, existingWiredSourcePortIds),
    [source.ports, source.cabled, sourceSessionByPortId, existingWiredSourcePortIds],
  );
  const targetVisuals = useMemo(
    () => buildVisuals(target.ports, target.cabled, targetSessionByPortId, existingWiredTargetPortIds),
    [target.ports, target.cabled, targetSessionByPortId, existingWiredTargetPortIds],
  );
  const visuals: Record<Side, Map<string, PortRowVisual>> = useMemo(
    () => ({ source: sourceVisuals, target: targetVisuals }),
    [sourceVisuals, targetVisuals],
  );

  // The same filtered lists PortColumn itself renders (issue #517 review
  // item 7): "Connect 1:1 in order" must only pair ports the user can
  // currently see, never reach past an active filter.
  const filteredSourcePorts = useMemo(
    () => filterPorts(source.ports, filter.source),
    [source.ports, filter.source],
  );
  const filteredTargetPorts = useMemo(
    () => filterPorts(target.ports, filter.target),
    [target.ports, filter.target],
  );

  const sourcePortIndex = useMemo(
    () => new Map(source.ports.map((p, i) => [p.id, i])),
    [source.ports],
  );
  const targetPortIndex = useMemo(
    () => new Map(target.ports.map((p, i) => [p.id, i])),
    [target.ports],
  );
  // Memoized (not a fresh object literal per render): this feeds a
  // useLayoutEffect dependency array below, and an unstable reference there
  // reruns the effect on every render, which calls setLineGeoms with a new
  // array reference every time and loops forever (issue #517 review fix:
  // this was reproduced as a real "Maximum update depth exceeded" crash).
  const portIndexBySide: Record<Side, Map<string, number>> = useMemo(
    () => ({ source: sourcePortIndex, target: targetPortIndex }),
    [sourcePortIndex, targetPortIndex],
  );

  const freeCounts = useMemo(
    () => ({
      source: source.ports.filter((p) => sourceVisuals.get(p.id)?.status === "free").length,
      target: target.ports.filter((p) => targetVisuals.get(p.id)?.status === "free").length,
    }),
    [source.ports, target.ports, sourceVisuals, targetVisuals],
  );

  const registerRowRef = useCallback((side: Side, portId: string, el: HTMLDivElement | null) => {
    if (el) rowRefs.current[side].set(portId, el);
    else rowRefs.current[side].delete(portId);
  }, []);

  // Stable per-side handlers (issue #517 review round 3 item 8), not a
  // curried `handleScroll(side)` called during render: calling a curried
  // function during render to produce the prop VALUE creates a brand new
  // function reference every render regardless of whether anything actually
  // changed, which defeats PortColumn's React.memo just as surely as an
  // inline arrow would (the whole point of memoizing PortColumn is that its
  // own props stay referentially stable across an unrelated re-render).
  const sourceScroll = useCallback((e: React.UIEvent<HTMLDivElement>) => {
    scrollTopRef.current.source = e.currentTarget.scrollTop;
    setScrollTick((t) => ({ ...t, source: t.source + 1 }));
  }, []);
  const targetScroll = useCallback((e: React.UIEvent<HTMLDivElement>) => {
    scrollTopRef.current.target = e.currentTarget.scrollTop;
    setScrollTick((t) => ({ ...t, target: t.target + 1 }));
  }, []);

  const bumpBothScrollTicks = useCallback(() => {
    setScrollTick((t) => ({ source: t.source + 1, target: t.target + 1 }));
  }, []);

  const completeConnection = useCallback(
    (a: Selection, b: Selection) => {
      const src = a.side === "source" ? a.port : b.port;
      const tgt = a.side === "target" ? a.port : b.port;
      const conn = buildSessionConnection(src, tgt, newLineLayer, source.cabled, target.cabled);
      setSessionConnections((prev) => [...prev, conn]);
      setPendingSource(null);
      setErrorMessage(null);
    },
    [newLineLayer, source.cabled, target.cabled],
  );

  // The single activation path (issue #517 review item 2): reused by a
  // genuine click (mousedown+mouseup with no movement, detected in the
  // window mouseup handler below) and by keyboard Enter/Space on a row, so
  // there is exactly one place that decides arm vs complete vs disarm.
  const handlePortActivate = useCallback(
    (side: Side, port: Port) => {
      if (connectionsLoading) return;
      const visual = visuals[side].get(port.id);
      if (visual?.status === "session-wired") {
        setErrorMessage(`${port.name} already has a line in this session`);
        return;
      }
      if (visual?.status === "canvas-wired") {
        setErrorMessage(`${port.name} is already connected on the canvas`);
        return;
      }
      if (!pendingSource) {
        setPendingSource({ side, port });
        setErrorMessage(null);
        return;
      }
      if (pendingSource.side === side) {
        setPendingSource(pendingSource.port.id === port.id ? null : { side, port });
        setErrorMessage(null);
        return;
      }
      // Re-validate the ARMED port's status before completing (issue #517
      // review item 6): it can have become unavailable since arming (e.g.
      // "Connect 1:1 in order" claimed it, or a drag completed a different
      // pair through it) without pendingSource being explicitly cleared. A
      // stale arm must never silently double-claim a port.
      const armedVisual = visuals[pendingSource.side].get(pendingSource.port.id);
      if (armedVisual?.status !== "free") {
        setPendingSource(null);
        setErrorMessage(`${pendingSource.port.name} is no longer available; pick a source port again`);
        return;
      }
      completeConnection(pendingSource, { side, port });
    },
    [connectionsLoading, visuals, pendingSource, completeConnection],
  );

  const handlePortMouseDown = useCallback((side: Side, port: Port, e: React.MouseEvent) => {
    pressRef.current = { side, port, startX: e.clientX, startY: e.clientY, moved: false };
  }, []);

  // Stable per-side wrappers, defined with their own useCallback rather than
  // as inline closures built inside a plain function called during render:
  // the latter pattern trips the react-hooks/refs lint rule (a function
  // created while rendering that calls into another function touching a
  // ref, even indirectly, is flagged as "may read a ref during render",
  // even though the actual ref access only ever happens later, inside the
  // real event handler or the row ref callback).
  const sourceMouseDown = useCallback(
    (port: Port, e: React.MouseEvent) => handlePortMouseDown("source", port, e),
    [handlePortMouseDown],
  );
  const targetMouseDown = useCallback(
    (port: Port, e: React.MouseEvent) => handlePortMouseDown("target", port, e),
    [handlePortMouseDown],
  );
  const sourceRegisterRowRef = useCallback(
    (portId: string, el: HTMLDivElement | null) => registerRowRef("source", portId, el),
    [registerRowRef],
  );
  const targetRegisterRowRef = useCallback(
    (portId: string, el: HTMLDivElement | null) => registerRowRef("target", portId, el),
    [registerRowRef],
  );

  // Global press tracker: a single mousedown/mousemove/mouseup path decides
  // click vs drag (issue #517 review item 2). The previous split
  // onMouseDown/onClick design re-armed on every mousedown and the window
  // mouseup handler treated a same-side mouseup (which happens on every
  // plain click, since mousedown and mouseup land on the same row) as an
  // always-cancel, so no click-to-connect pair ever completed. Re-subscribes
  // whenever the data it closes over changes; pressRef itself is untouched
  // by that, so an in-flight press survives a resubscription.
  useEffect(() => {
    function onMove(e: MouseEvent) {
      const press = pressRef.current;
      if (!press || press.moved) return;
      const dx = e.clientX - press.startX;
      const dy = e.clientY - press.startY;
      if (Math.hypot(dx, dy) > MOVE_THRESHOLD) {
        press.moved = true;
        setDragFrom({ side: press.side, port: press.port });
      }
    }

    function onUp(e: MouseEvent) {
      const press = pressRef.current;
      if (!press) return;
      pressRef.current = null;
      setDragFrom(null);

      if (press.moved) {
        if (connectionsLoading) return;
        const originVisual = visuals[press.side].get(press.port.id);
        if (originVisual?.status !== "free") return;
        const el = (e.target as HTMLElement)?.closest?.("[data-port-id]") as HTMLElement | null;
        if (el) {
          const portId = el.getAttribute("data-port-id");
          const side = el.getAttribute("data-side") as Side | null;
          const status = el.getAttribute("data-status");
          if (portId && side && side !== press.side && status === "free") {
            const port = sideData[side].ports.find((p) => p.id === portId);
            if (port) {
              completeConnection({ side: press.side, port: press.port }, { side, port });
              return;
            }
          }
        }
        // Failed drop: cancel the drag and drop any prior arm too, matching
        // the "click the armed port again disarms" spirit of a reset.
        setPendingSource(null);
        return;
      }

      // Not moved: a click. Same activation path as keyboard.
      handlePortActivate(press.side, press.port);
    }

    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    return () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
  }, [connectionsLoading, visuals, sideData, completeConnection, handlePortActivate]);

  // Recompute session-line endpoints from the currently mounted row refs. A
  // port scrolled out of view or filtered out has no ref: its end of the
  // line clamps to the wiring area's top or bottom edge, whichever its
  // estimated (index * row height - scroll offset) position falls past,
  // rather than freezing at the vertical center.
  useLayoutEffect(() => {
    const area = areaRef.current;
    if (!area) return;
    const areaRect = area.getBoundingClientRect();

    function endpointY(side: Side, portId: string): { y: number; clamped: boolean } {
      const el = rowRefs.current[side].get(portId);
      if (el) {
        const r = el.getBoundingClientRect();
        return { y: r.top + r.height / 2 - areaRect.top, clamped: false };
      }
      const idx = portIndexBySide[side].get(portId) ?? 0;
      const estimatedTop = idx * ROW_HEIGHT - scrollTopRef.current[side];
      return { y: estimatedTop < 0 ? 0 : areaRect.height, clamped: true };
    }

    const geoms: LineGeom[] = sessionConnections.map((conn) => {
      const s = endpointY("source", conn.sourcePortId);
      const t = endpointY("target", conn.targetPortId);
      return { id: conn.id, y1: s.y, y2: t.y, sourceClamped: s.clamped, targetClamped: t.clamped };
    });
    setAreaWidth(areaRect.width);
    // A previous version of this effect guarded this call with a manual
    // deep-compare (lineGeomsEqual) because an earlier, unmemoized
    // usePortAvailability plus a test mock that fabricated a fresh `[]` per
    // call made `portIndexBySide` churn every render, which made THIS
    // effect rerun every render, which made it call setLineGeoms with a
    // new-but-equal array every time: a real, reproduced infinite render
    // loop (issue #517 review). usePortAvailability now memoizes its return
    // properly (review item 10b) and the test fixtures reuse stable empty
    // references the way real TanStack Query does, so `portIndexBySide` (and
    // every other dependency below) is only unstable when something
    // genuinely changed; the deep-compare guard was proven unnecessary by
    // deleting it and confirming the full suite, including the exact test
    // that reproduced the crash, still passes.
    setLineGeoms(geoms);
  }, [sessionConnections, filter.source, filter.target, scrollTick.source, scrollTick.target, portIndexBySide]);

  // The wiring area's size changes when the review strip toggles or the
  // window resizes; either must recompute line geometry (issue #517 review
  // item 9), not just scroll/filter changes.
  useEffect(() => {
    const area = areaRef.current;
    if (!area || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => bumpBothScrollTicks());
    observer.observe(area);
    return () => observer.disconnect();
  }, [bumpBothScrollTicks]);

  const lineGeomById = useMemo(() => new Map(lineGeoms.map((g) => [g.id, g])), [lineGeoms]);

  const handleLayerChange = useCallback((lineId: string, layer: EdgeLayerType) => {
    setSessionConnections((prev) => prev.map((c) => (c.id === lineId ? { ...c, layer } : c)));
    setNewLineLayer(layer);
  }, []);

  const handleRemoveLine = useCallback((lineId: string) => {
    setSessionConnections((prev) => prev.filter((c) => c.id !== lineId));
    setSelectedLineId((current) => (current === lineId ? null : current));
  }, []);

  const handleConnectOneToOne = useCallback(() => {
    if (connectionsLoading) return;
    const freeSource = filteredSourcePorts.filter((p) => sourceVisuals.get(p.id)?.status === "free");
    const freeTarget = filteredTargetPorts.filter((p) => targetVisuals.get(p.id)?.status === "free");
    const n = Math.min(freeSource.length, freeTarget.length);
    if (n === 0) {
      setErrorMessage("No free ports left to pair");
      return;
    }
    const newConns: SessionConnection[] = [];
    for (let i = 0; i < n; i++) {
      newConns.push(buildSessionConnection(freeSource[i], freeTarget[i], newLineLayer, source.cabled, target.cabled));
    }
    setSessionConnections((prev) => [...prev, ...newConns]);
    // Whatever was armed may now be part of the freshly-wired batch above;
    // clear it rather than leave a stale arm around (issue #517 review item
    // 6). Left alone on the "nothing to pair" error path above: an error
    // should not blow away the user's current in-progress selection.
    setPendingSource(null);
    setErrorMessage(null);
  }, [
    connectionsLoading,
    filteredSourcePorts,
    filteredTargetPorts,
    sourceVisuals,
    targetVisuals,
    source.cabled,
    target.cabled,
    newLineLayer,
  ]);

  const handleConfirm = useCallback(() => {
    if (sessionConnections.length === 0 || connectionsLoading) return;
    onConfirm(sessionConnections);
  }, [sessionConnections, connectionsLoading, onConfirm]);

  const handleAreaClick = useCallback((e: React.MouseEvent) => {
    if (e.target === e.currentTarget) setSelectedLineId(null);
  }, []);

  const armedOrDraggedSide = pendingSource?.side ?? dragFrom?.side ?? null;
  const highlightForSide = (side: Side) => armedOrDraggedSide !== null && armedOrDraggedSide !== side;
  const armedPortIdForSide = (side: Side) =>
    pendingSource?.side === side ? pendingSource.port.id : dragFrom?.side === side ? dragFrom.port.id : null;

  const hint = dragFrom
    ? "Release on a highlighted port"
    : pendingSource
      ? `${pendingSource.port.name} selected. Click a target port to connect`
      : "Drag from a source port to a target port, or click one then the other";

  const confirmLabel = useMemo(() => {
    if (sessionConnections.length === 0) return "Add connections";
    // The "(keeps 1 per pair)" caveat (issue #517 review item 4) is gone: issue
    // #531 made resolve_canvas_wiring honor each staged line's own ports, so
    // provisioning no longer collapses same-pair lines to one wire.
    return sessionConnections.length === 1
      ? "Add 1 connection"
      : `Add ${sessionConnections.length} connections`;
  }, [sessionConnections.length]);

  // Data-only column props (issue #517 review item 10's collapse), kept
  // deliberately separate from the ref-touching callback props below: the
  // react-hooks/refs lint rule flags a ref-reading callback merely being
  // referenced from inside a plain function invoked during render (even
  // wrapped in its own useCallback further up), so those four props are
  // assigned directly in each <PortColumn> tag instead of threaded through
  // this helper.
  const sourceFilterChange = useCallback(
    (value: string) => setFilterState((f) => ({ ...f, source: value })),
    [],
  );
  const targetFilterChange = useCallback(
    (value: string) => setFilterState((f) => ({ ...f, target: value })),
    [],
  );
  const filteredPorts: Record<Side, Port[]> = { source: filteredSourcePorts, target: filteredTargetPorts };
  const filterChangeForSide: Record<Side, (value: string) => void> = {
    source: sourceFilterChange,
    target: targetFilterChange,
  };

  function columnDataProps(side: Side) {
    return {
      side,
      deviceName: deviceName[side],
      topologyType: topologyType[side],
      ports: filteredPorts[side],
      totalCount: sideData[side].ports.length,
      isLoading: sideData[side].isLoading,
      visuals: visuals[side],
      cabledCount: sideData[side].cabled.size,
      filter: filter[side],
      onFilterChange: filterChangeForSide[side],
      armedPortId: armedPortIdForSide(side),
      highlightFree: highlightForSide(side),
      interactionDisabled: connectionsLoading,
    };
  }
  const sourceActivate = useCallback((port: Port) => handlePortActivate("source", port), [handlePortActivate]);
  const targetActivate = useCallback((port: Port) => handlePortActivate("target", port), [handlePortActivate]);

  return (
    <Modal
      open={open}
      onClose={onCancel}
      title={`Wire ${sourceDeviceName} to ${targetDeviceName}`}
      className="max-w-[824px]"
      bodyClassName="p-0"
    >
      <div className="flex flex-col max-h-[calc(96vh-56px)]">
        <div className="flex flex-1 min-h-0">
          <PortColumn
            {...columnDataProps("source")}
            onPortMouseDown={sourceMouseDown}
            onPortActivate={sourceActivate}
            registerRowRef={sourceRegisterRowRef}
            onScroll={sourceScroll}
          />

          {/* Middle wiring area */}
          <div className="flex-1 flex flex-col border-x border-gray-200 min-w-[240px]">
            <div className="h-[94px] shrink-0 bg-gray-50 border-b border-gray-200 px-3 py-2 flex flex-col gap-2">
              <div className="flex items-center gap-2">
                <span className="text-xs font-medium text-gray-600">New lines</span>
                <LayerSegmentedControl
                  value={newLineLayer}
                  onChange={setNewLineLayer}
                  size="sm"
                  testIdPrefix="new-line-layer"
                  title={LAYER_TOOLTIP}
                />
              </div>
              <p className="text-[11px] text-gray-500">{hint}</p>
            </div>

            <div
              ref={areaRef}
              onClick={handleAreaClick}
              className="relative flex-1 min-h-[220px] overflow-hidden bg-white"
            >
              {sessionConnections.length === 0 ? (
                <p className="absolute inset-0 flex items-center justify-center text-xs text-gray-400">
                  No connections yet
                </p>
              ) : (
                <svg className="absolute inset-0 w-full h-full pointer-events-none" aria-hidden="true">
                  {sessionConnections.map((c) => {
                    const g = lineGeomById.get(c.id);
                    if (!g) return null;
                    const style = LAYER_STYLES[c.layer];
                    const opacity = g.sourceClamped || g.targetClamped ? 0.35 : 1;
                    const midX = areaWidth / 2;
                    return (
                      <path
                        key={g.id}
                        data-testid={`wiring-line-${g.id}`}
                        d={`M 0 ${g.y1} C ${midX} ${g.y1}, ${midX} ${g.y2}, ${areaWidth} ${g.y2}`}
                        stroke={style.stroke}
                        strokeWidth={2}
                        strokeDasharray={style.strokeDasharray}
                        fill="none"
                        opacity={opacity}
                      />
                    );
                  })}
                </svg>
              )}

              {sessionConnections.map((c) => {
                const geom = lineGeomById.get(c.id);
                const midY = geom ? (geom.y1 + geom.y2) / 2 : 0;
                const isSelected = selectedLineId === c.id;
                return (
                  <div
                    key={c.id}
                    style={{ position: "absolute", left: "50%", top: midY, transform: "translate(-50%, -50%)" }}
                    className="pointer-events-auto"
                  >
                    {isSelected ? (
                      <div className="flex items-center gap-1 bg-white border border-gray-200 rounded-md shadow-xl px-1 py-1">
                        <LayerSegmentedControl
                          value={c.layer}
                          onChange={(l) => handleLayerChange(c.id, l)}
                          size="xs"
                          testIdPrefix={`line-layer-${c.id}`}
                          title={LAYER_TOOLTIP}
                        />
                        <div className="w-px h-4 bg-gray-200 mx-0.5" />
                        <button
                          type="button"
                          onClick={() => handleRemoveLine(c.id)}
                          aria-label={`Delete line ${c.sourcePortName} to ${c.targetPortName}`}
                          className="p-1 rounded text-gray-500 hover:text-red-600 hover:bg-red-50"
                        >
                          <Trash2 className="w-3.5 h-3.5" />
                        </button>
                      </div>
                    ) : (
                      <LayerBadge layer={c.layer} onClick={() => setSelectedLineId(c.id)} testId={`line-pill-${c.id}`} />
                    )}
                  </div>
                );
              })}
            </div>
          </div>

          <PortColumn
            {...columnDataProps("target")}
            onPortMouseDown={targetMouseDown}
            onPortActivate={targetActivate}
            registerRowRef={targetRegisterRowRef}
            onScroll={targetScroll}
          />
        </div>

        {reviewOpen && (
          <div
            data-testid="review-strip"
            className="bg-gray-50 border-t border-gray-200 max-h-[168px] overflow-y-auto"
          >
            {sessionConnections.map((c) => {
              // The pre-confirm signal restored (issue #517 review round 3
              // item 6): pathValid is unresolved pre-confirm (the edge is
              // not on the canvas yet), so this only ever surfaces the
              // uncabled case, but that is exactly what the review strip
              // should show before the user commits.
              const status = resolveEdgeStroke({ layer: c.layer, portsCabled: c.portsCabled });
              return (
                <div
                  key={c.id}
                  className="flex items-center gap-2 px-3 py-1 text-xs border-b border-gray-100 last:border-b-0"
                >
                  <span className="font-mono">{c.sourcePortName}</span>
                  <span className="text-gray-400">to</span>
                  <span className="font-mono">{c.targetPortName}</span>
                  <LayerBadge layer={c.layer} strokeOverride={status.stroke} />
                  {status.statusLabel && (
                    <span className="text-[10px]" style={{ color: status.stroke }}>
                      {status.statusLabel}
                    </span>
                  )}
                  <button
                    type="button"
                    onClick={() => handleRemoveLine(c.id)}
                    aria-label={`Remove ${c.sourcePortName} to ${c.targetPortName}`}
                    className="ml-auto text-gray-400 hover:text-gray-600"
                  >
                    &times;
                  </button>
                </div>
              );
            })}
          </div>
        )}

        <div className="flex items-center gap-3 px-4 py-3 border-t border-gray-200">
          <div className="flex-1 min-w-0">
            <p className="text-xs text-gray-500">
              Free ports: {freeCounts.source} source, {freeCounts.target} target
            </p>
            <p data-testid="layer-annotation-note" className="text-[11px] text-gray-500 mt-0.5">
              Layer is recorded on the canvas per line; provisioning derives L2 and L3 from the
              resolved path.
            </p>
            {errorMessage && <p className="text-xs text-red-600 mt-0.5">{errorMessage}</p>}
          </div>
          <Button variant="ghost" size="sm" onClick={() => setReviewOpen((v) => !v)}>
            Review ({sessionConnections.length})
          </Button>
          <Button variant="outline" size="sm" onClick={handleConnectOneToOne} disabled={connectionsLoading}>
            Connect 1:1 in order
          </Button>
          <Button variant="secondary" size="sm" onClick={onCancel}>
            Cancel
          </Button>
          <Button
            variant="primary"
            size="sm"
            onClick={handleConfirm}
            disabled={sessionConnections.length === 0 || connectionsLoading}
          >
            {confirmLabel}
          </Button>
        </div>
      </div>
    </Modal>
  );
}
