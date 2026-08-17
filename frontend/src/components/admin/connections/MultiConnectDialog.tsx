import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Trash2 } from "lucide-react";
import { Modal } from "@/components/ui/Modal";
import { Button } from "@/components/ui/Button";
import { BULK_CONNECTION_LIMIT, useDeviceConnections } from "@/api/connections";
import { usePortAvailability } from "@/components/topology-editor/wiring/usePortAvailability";
import {
  PortColumn,
  ROW_HEIGHT,
  type PortRowVisual,
  type Side,
} from "@/components/topology-editor/wiring/PortColumn";
import { filterPorts } from "@/components/topology-editor/wiring/filterPorts";
import { DeviceSearchPicker, type PickedDevice } from "./DeviceSearchPicker";
import {
  applyBulkResult,
  buildBulkItems,
  existingPairKeys,
  pairKey,
  portKey,
  type StagedLine,
} from "./bulkStaging";
import { errorDetail } from "@/lib/errors";
import { genId } from "@/lib/id";
import type {
  BulkConnectionResult,
  Connection,
  ConnectionCreate,
} from "@/types/connection.types";
import type { Port } from "@/types/port.types";

export interface MultiConnectDialogProps {
  open: boolean;
  /** Submits the batch; resolves with the row-level result (200 even on rejections). */
  onSubmit: (items: ConnectionCreate[]) => Promise<BulkConnectionResult>;
  /** Called only when EVERY row was created, with the created count. */
  onSuccess: (created: number) => void;
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

interface LineGeom {
  id: string;
  y1: number;
  y2: number;
  sourceClamped: boolean;
  targetClamped: boolean;
}

// Pointer movement past this many pixels between mousedown and mouseup turns
// the gesture into a drag; short of that it is a click. Mirrors WiringDialog's
// arbitration: one global mousemove/mouseup pair, in-flight press state in a
// ref rather than reactive state.
const MOVE_THRESHOLD = 4;

// Module-level stable references (the issue #517 lesson): a fresh `[]` or
// `new Set()` default evaluated per render feeds unstable values into the memo
// and layout-effect dependency chains below, which reruns the geometry effect
// every render, which calls setLineGeoms every render: an infinite render loop
// jsdom does not catch. Every "no data yet" case must resolve to the SAME
// object reference.
const NO_CONNECTIONS: Connection[] = [];
const NO_PAIR_KEYS: ReadonlySet<string> = new Set<string>();

// Plain pixel colors, no layer vocabulary: a registered cable carries a
// connection_type string, not an OSI layer.
const LINE_STROKE = "#2563eb";
const LINE_STROKE_DUPLICATE = "#d97706";
const LINE_STROKE_REJECTED = "#dc2626";

function buildVisuals(
  ports: Port[],
  cabled: Set<string>,
  deviceId: string | undefined,
  usedPortKeys: Set<string>,
): Map<string, PortRowVisual> {
  const map = new Map<string, PortRowVisual>();
  for (const port of ports) {
    map.set(port.id, {
      // The ONE hard block: a port already claimed by another line in this
      // staging session. Cabling state never blocks (warn, never block).
      status: usedPortKeys.has(portKey(deviceId, port.id)) ? "session-wired" : "free",
      cabled: cabled.has(port.name),
    });
  }
  return map;
}

function strokeFor(line: StagedLine): string {
  if (line.error) return LINE_STROKE_REJECTED;
  if (line.duplicate) return LINE_STROKE_DUPLICATE;
  return LINE_STROKE;
}

function EmptyColumn({ side }: { side: Side }) {
  return (
    <div className="flex flex-col w-[240px] shrink-0" data-testid={`port-column-${side}-empty`}>
      <div className="h-[94px] shrink-0 bg-gray-50 border-b border-gray-200" />
      <p className="text-xs text-gray-400 p-2">No device selected</p>
    </div>
  );
}

/**
 * Admin multi-connection dialog: pick two devices, stage many cables between
 * them across two virtualized port columns, then register the whole batch in
 * one POST /cabling/connections/bulk.
 *
 * Conflict policy is warn, never block. A port carrying a registered cable and
 * a pair duplicating an existing connection are both flagged and both fully
 * confirmable, because the cabling service deliberately permits duplicates and
 * double-cabled ports. The only hard block is a port already used by another
 * line in the CURRENT staging session.
 *
 * The gesture arbitration, line geometry, and column plumbing deliberately
 * mirror the topology editor's WiringDialog and reuse its wiring/ primitives
 * unchanged; the duplicated shell is tracked for extraction as a follow-up.
 */
export function MultiConnectDialog({ open, onSubmit, onSuccess, onCancel }: MultiConnectDialogProps) {
  const [deviceA, setDeviceA] = useState<PickedDevice | null>(null);
  const [deviceB, setDeviceB] = useState<PickedDevice | null>(null);

  const { source, target, connectionsLoading } = usePortAvailability(
    deviceA?.id ?? "",
    deviceB?.id ?? "",
  );
  const sideData: Record<Side, typeof source> = useMemo(
    () => ({ source, target }),
    [source, target],
  );

  // Same query key as usePortAvailability's own A-side call, so this is a
  // cache read, not a second request. usePortAvailability reduces connections
  // to cabled port NAMES; duplicate detection needs the raw pairs.
  const { data: deviceAConnections } = useDeviceConnections(deviceA?.id);

  const [lines, setLines] = useState<StagedLine[]>([]);
  const [pendingSource, setPendingSource] = useState<Selection | null>(null);
  const [dragFrom, setDragFrom] = useState<Selection | null>(null);
  const [filter, setFilterState] = useState<Record<Side, string>>({ source: "", target: "" });
  const [selectedLineId, setSelectedLineId] = useState<string | null>(null);
  const [reviewOpen, setReviewOpen] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [summary, setSummary] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [connectionType, setConnectionType] = useState("ethernet");
  const [notes, setNotes] = useState("");
  const [lineGeoms, setLineGeoms] = useState<LineGeom[]>([]);
  // Numeric pixel width for the SVG path data. A percentage inside a path `d`
  // is not valid path syntax at all; jsdom never parses it, so that class of
  // bug only shows in a real browser.
  const [areaWidth, setAreaWidth] = useState(0);
  const [scrollTick, setScrollTick] = useState<Record<Side, number>>({ source: 0, target: 0 });

  const areaRef = useRef<HTMLDivElement>(null);
  const rowRefs = useRef<Record<Side, Map<string, HTMLDivElement>>>({
    source: new Map(),
    target: new Map(),
  });
  const scrollTopRef = useRef<Record<Side, number>>({ source: 0, target: 0 });
  const pressRef = useRef<PressState | null>(null);

  const existingKeys = useMemo(() => {
    if (!deviceA || !deviceB) return NO_PAIR_KEYS;
    return existingPairKeys(deviceAConnections ?? NO_CONNECTIONS, deviceA.id, deviceB.id);
  }, [deviceAConnections, deviceA, deviceB]);

  // Keyed by device AND port so picking the same device on both sides shares
  // one claimed-port set instead of two independent per-side ones.
  const usedPortKeys = useMemo(() => {
    const keys = new Set<string>();
    for (const line of lines) {
      keys.add(portKey(deviceA?.id, line.sourcePortId));
      keys.add(portKey(deviceB?.id, line.targetPortId));
    }
    return keys;
  }, [lines, deviceA, deviceB]);

  const sourceVisuals = useMemo(
    () => buildVisuals(source.ports, source.cabled, deviceA?.id, usedPortKeys),
    [source.ports, source.cabled, deviceA, usedPortKeys],
  );
  const targetVisuals = useMemo(
    () => buildVisuals(target.ports, target.cabled, deviceB?.id, usedPortKeys),
    [target.ports, target.cabled, deviceB, usedPortKeys],
  );
  const visuals: Record<Side, Map<string, PortRowVisual>> = useMemo(
    () => ({ source: sourceVisuals, target: targetVisuals }),
    [sourceVisuals, targetVisuals],
  );

  // The exact lists PortColumn renders: "Connect 1:1 in order" pairs only what
  // the user can currently see, never reaching past an active filter.
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

  const duplicateCount = useMemo(() => lines.filter((l) => l.duplicate).length, [lines]);
  const interactionDisabled = connectionsLoading || submitting;

  const registerRowRef = useCallback((side: Side, portId: string, el: HTMLDivElement | null) => {
    if (el) rowRefs.current[side].set(portId, el);
    else rowRefs.current[side].delete(portId);
  }, []);

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

  const buildLine = useCallback(
    (src: Port, tgt: Port): StagedLine => ({
      id: genId(),
      sourcePortId: src.id,
      sourcePortName: src.name,
      targetPortId: tgt.id,
      targetPortName: tgt.name,
      duplicate: existingKeys.has(pairKey(src.name, tgt.name)),
      sourceCabled: source.cabled.has(src.name),
      targetCabled: target.cabled.has(tgt.name),
      error: null,
    }),
    [existingKeys, source.cabled, target.cabled],
  );

  const completeConnection = useCallback(
    (a: Selection, b: Selection) => {
      const src = a.side === "source" ? a.port : b.port;
      const tgt = a.side === "target" ? a.port : b.port;
      // Only reachable when the same device is picked on both sides; the
      // backend rejects it with this exact wording, so refuse it up front
      // rather than spending a batch row on a guaranteed rejection.
      if (src.id === tgt.id) {
        setPendingSource(null);
        setErrorMessage("Cannot connect a port to itself");
        return;
      }
      setLines((prev) => [...prev, buildLine(src, tgt)]);
      setPendingSource(null);
      setErrorMessage(null);
    },
    [buildLine],
  );

  // The single activation path, shared by a genuine click (mousedown+mouseup
  // with no movement) and keyboard Enter/Space on a row.
  const handlePortActivate = useCallback(
    (side: Side, port: Port) => {
      if (interactionDisabled) return;
      const visual = visuals[side].get(port.id);
      if (visual?.status === "session-wired") {
        setErrorMessage(`${port.name} already has a line in this session`);
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
      // Re-validate the ARMED port before completing: it can have gone stale
      // since arming (for instance "Connect 1:1 in order" claimed it) without
      // pendingSource being explicitly cleared.
      const armedVisual = visuals[pendingSource.side].get(pendingSource.port.id);
      if (armedVisual?.status !== "free") {
        setPendingSource(null);
        setErrorMessage(`${pendingSource.port.name} is no longer available; pick a source port again`);
        return;
      }
      completeConnection(pendingSource, { side, port });
    },
    [interactionDisabled, visuals, pendingSource, completeConnection],
  );

  const handlePortMouseDown = useCallback((side: Side, port: Port, e: React.MouseEvent) => {
    pressRef.current = { side, port, startX: e.clientX, startY: e.clientY, moved: false };
  }, []);

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

  // One global press tracker decides click vs drag; pressRef survives the
  // resubscriptions this effect goes through when its closed-over data changes.
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
        if (interactionDisabled) return;
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
        setPendingSource(null);
        return;
      }

      handlePortActivate(press.side, press.port);
    }

    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    return () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
  }, [interactionDisabled, visuals, sideData, completeConnection, handlePortActivate]);

  // Endpoints come from the mounted row refs; a port scrolled or filtered out
  // of view has no ref and clamps to whichever edge its estimated position
  // (index * ROW_HEIGHT - scroll offset) falls past.
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

    const geoms: LineGeom[] = lines.map((line) => {
      const s = endpointY("source", line.sourcePortId);
      const t = endpointY("target", line.targetPortId);
      return { id: line.id, y1: s.y, y2: t.y, sourceClamped: s.clamped, targetClamped: t.clamped };
    });
    setAreaWidth(areaRect.width);
    setLineGeoms(geoms);
  }, [lines, filter.source, filter.target, scrollTick.source, scrollTick.target, portIndexBySide]);

  useEffect(() => {
    const area = areaRef.current;
    if (!area || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => bumpBothScrollTicks());
    observer.observe(area);
    return () => observer.disconnect();
  }, [bumpBothScrollTicks]);

  const lineGeomById = useMemo(() => new Map(lineGeoms.map((g) => [g.id, g])), [lineGeoms]);

  const handleRemoveLine = useCallback((lineId: string) => {
    setLines((prev) => prev.filter((l) => l.id !== lineId));
    setSelectedLineId((current) => (current === lineId ? null : current));
  }, []);

  const handleConnectOneToOne = useCallback(() => {
    if (interactionDisabled) return;
    if (!deviceA || !deviceB) {
      setErrorMessage("Pick a device on each side first");
      return;
    }
    const freeSource = filteredSourcePorts.filter((p) => sourceVisuals.get(p.id)?.status === "free");
    const freeTarget = filteredTargetPorts.filter((p) => targetVisuals.get(p.id)?.status === "free");
    const n = Math.min(freeSource.length, freeTarget.length);
    if (n === 0) {
      setErrorMessage("No free ports left to pair");
      return;
    }
    const added: StagedLine[] = [];
    for (let i = 0; i < n; i++) {
      // Same-device selection lines both columns up identically, so pairing in
      // order would connect every port to itself; skip those rows.
      if (freeSource[i].id === freeTarget[i].id) continue;
      added.push(buildLine(freeSource[i], freeTarget[i]));
    }
    if (added.length === 0) {
      setErrorMessage("No free ports left to pair");
      return;
    }
    setLines((prev) => [...prev, ...added]);
    setPendingSource(null);
    setErrorMessage(null);
  }, [
    interactionDisabled,
    deviceA,
    deviceB,
    filteredSourcePorts,
    filteredTargetPorts,
    sourceVisuals,
    targetVisuals,
    buildLine,
  ]);

  const handleConfirm = useCallback(async () => {
    if (!deviceA || !deviceB || lines.length === 0 || interactionDisabled) return;
    if (lines.length > BULK_CONNECTION_LIMIT) {
      setErrorMessage(`Batches are capped at ${BULK_CONNECTION_LIMIT} connections; remove some lines`);
      return;
    }
    setSubmitting(true);
    setErrorMessage(null);
    try {
      const items = buildBulkItems(lines, deviceA.id, deviceB.id, connectionType, notes);
      const result = await onSubmit(items);
      const outcome = applyBulkResult(lines, result);
      if (outcome.remaining.length === 0) {
        setLines([]);
        setSummary(null);
        onSuccess(result.created);
        return;
      }
      // Partial success: the created rows drop out of staging (re-submitting
      // them would duplicate real cabling, which the backend permits), the
      // rejected ones stay with their reason so a retry sends only failures.
      setLines(outcome.remaining);
      setSummary(outcome.summary);
      setSelectedLineId(null);
      setReviewOpen(true);
    } catch (err: unknown) {
      setErrorMessage(errorDetail(err, "Failed to create connections"));
    } finally {
      setSubmitting(false);
    }
  }, [deviceA, deviceB, lines, interactionDisabled, connectionType, notes, onSubmit, onSuccess]);

  const handleAreaClick = useCallback((e: React.MouseEvent) => {
    if (e.target === e.currentTarget) setSelectedLineId(null);
  }, []);

  const handleDeviceChange = useCallback(
    (side: Side, device: PickedDevice | null) => {
      if (side === "source") setDeviceA(device);
      else setDeviceB(device);
      setPendingSource(null);
      setSelectedLineId(null);
      setSummary(null);
      setFilterState((f) => ({ ...f, [side]: "" }));
      setErrorMessage(
        lines.length > 0 ? "Staged connections were cleared because the device changed" : null,
      );
      setLines([]);
    },
    [lines.length],
  );

  const selectSourceDevice = useCallback(
    (device: PickedDevice) => handleDeviceChange("source", device),
    [handleDeviceChange],
  );
  const clearSourceDevice = useCallback(() => handleDeviceChange("source", null), [handleDeviceChange]);
  const selectTargetDevice = useCallback(
    (device: PickedDevice) => handleDeviceChange("target", device),
    [handleDeviceChange],
  );
  const clearTargetDevice = useCallback(() => handleDeviceChange("target", null), [handleDeviceChange]);

  const sourceFilterChange = useCallback(
    (value: string) => setFilterState((f) => ({ ...f, source: value })),
    [],
  );
  const targetFilterChange = useCallback(
    (value: string) => setFilterState((f) => ({ ...f, target: value })),
    [],
  );
  const sourceActivate = useCallback(
    (port: Port) => handlePortActivate("source", port),
    [handlePortActivate],
  );
  const targetActivate = useCallback(
    (port: Port) => handlePortActivate("target", port),
    [handlePortActivate],
  );

  const armedOrDraggedSide = pendingSource?.side ?? dragFrom?.side ?? null;
  const highlightForSide = (side: Side) => armedOrDraggedSide !== null && armedOrDraggedSide !== side;
  const armedPortIdForSide = (side: Side) =>
    pendingSource?.side === side
      ? pendingSource.port.id
      : dragFrom?.side === side
        ? dragFrom.port.id
        : null;

  const hint = !deviceA || !deviceB
    ? "Pick a device on each side to list its ports"
    : dragFrom
      ? "Release on a highlighted port"
      : pendingSource
        ? `${pendingSource.port.name} selected. Click a port on the other side to connect`
        : "Drag from one port to another, or click one then the other";

  const confirmLabel = submitting
    ? "Creating..."
    : lines.length === 0
      ? "Create connections"
      : lines.length === 1
        ? "Create 1 connection"
        : `Create ${lines.length} connections`;

  const filteredPorts: Record<Side, Port[]> = { source: filteredSourcePorts, target: filteredTargetPorts };
  const devices: Record<Side, PickedDevice | null> = { source: deviceA, target: deviceB };
  const filterChangeForSide: Record<Side, (value: string) => void> = {
    source: sourceFilterChange,
    target: targetFilterChange,
  };

  function columnDataProps(side: Side) {
    const device = devices[side];
    return {
      side,
      deviceName: device?.name ?? "",
      topologyType: device?.topologyType ?? ("PHYSICAL" as const),
      ports: filteredPorts[side],
      totalCount: sideData[side].ports.length,
      isLoading: sideData[side].isLoading,
      visuals: visuals[side],
      cabledCount: sideData[side].cabled.size,
      filter: filter[side],
      onFilterChange: filterChangeForSide[side],
      armedPortId: armedPortIdForSide(side),
      highlightFree: highlightForSide(side),
      interactionDisabled,
    };
  }

  return (
    <Modal
      open={open}
      onClose={onCancel}
      title="Create multiple connections"
      className="max-w-[880px]"
      bodyClassName="p-0"
    >
      <div className="flex flex-col max-h-[calc(96vh-56px)]">
        <div className="flex items-center bg-gray-50 border-b border-gray-200">
          <div className="w-[240px] shrink-0 px-2 py-2">
            <DeviceSearchPicker
              label="Device A"
              device={deviceA}
              onSelect={selectSourceDevice}
              onClear={clearSourceDevice}
            />
          </div>
          <div className="flex-1 min-w-[240px] px-3 py-2 border-x border-gray-200">
            <p className="text-[11px] text-gray-500">
              Register many cables between two devices in one batch
            </p>
          </div>
          <div className="w-[240px] shrink-0 px-2 py-2">
            <DeviceSearchPicker
              label="Device B"
              device={deviceB}
              onSelect={selectTargetDevice}
              onClear={clearTargetDevice}
            />
          </div>
        </div>

        <div className="flex flex-1 min-h-0">
          {deviceA ? (
            <PortColumn
              {...columnDataProps("source")}
              onPortMouseDown={sourceMouseDown}
              onPortActivate={sourceActivate}
              registerRowRef={sourceRegisterRowRef}
              onScroll={sourceScroll}
            />
          ) : (
            <EmptyColumn side="source" />
          )}

          <div className="flex-1 flex flex-col border-x border-gray-200 min-w-[240px]">
            <div className="h-[94px] shrink-0 bg-gray-50 border-b border-gray-200 px-3 py-2 flex flex-col gap-2">
              <span className="text-xs font-medium text-gray-600">Staged connections</span>
              <p className="text-[11px] text-gray-500">{hint}</p>
            </div>

            <div
              ref={areaRef}
              onClick={handleAreaClick}
              className="relative flex-1 min-h-[220px] overflow-hidden bg-white"
            >
              {lines.length === 0 ? (
                <p className="absolute inset-0 flex items-center justify-center text-xs text-gray-400">
                  No connections staged
                </p>
              ) : (
                <svg className="absolute inset-0 w-full h-full pointer-events-none" aria-hidden="true">
                  {lines.map((line) => {
                    const g = lineGeomById.get(line.id);
                    if (!g) return null;
                    const opacity = g.sourceClamped || g.targetClamped ? 0.35 : 1;
                    const midX = areaWidth / 2;
                    return (
                      <path
                        key={g.id}
                        data-testid={`staged-line-${g.id}`}
                        d={`M 0 ${g.y1} C ${midX} ${g.y1}, ${midX} ${g.y2}, ${areaWidth} ${g.y2}`}
                        stroke={strokeFor(line)}
                        strokeWidth={2}
                        strokeDasharray={line.duplicate || line.error ? "4 3" : undefined}
                        fill="none"
                        opacity={opacity}
                      />
                    );
                  })}
                </svg>
              )}

              {lines.map((line) => {
                const geom = lineGeomById.get(line.id);
                const midY = geom ? (geom.y1 + geom.y2) / 2 : 0;
                const isSelected = selectedLineId === line.id;
                const flag = line.error ? "REJECTED" : line.duplicate ? "DUPLICATE" : null;
                return (
                  <div
                    key={line.id}
                    style={{ position: "absolute", left: "50%", top: midY, transform: "translate(-50%, -50%)" }}
                    className="pointer-events-auto"
                  >
                    {isSelected ? (
                      <div className="flex items-center gap-1 bg-white border border-gray-200 rounded-md shadow-xl px-2 py-1">
                        <span className="font-mono text-[11px] text-gray-700">
                          {line.sourcePortName} to {line.targetPortName}
                        </span>
                        <button
                          type="button"
                          onClick={() => handleRemoveLine(line.id)}
                          aria-label={`Delete line ${line.sourcePortName} to ${line.targetPortName}`}
                          className="p-1 rounded text-gray-500 hover:text-red-600 hover:bg-red-50"
                        >
                          <Trash2 className="w-3.5 h-3.5" />
                        </button>
                      </div>
                    ) : (
                      <button
                        type="button"
                        data-testid={`line-pill-${line.id}`}
                        aria-label={`Select line ${line.sourcePortName} to ${line.targetPortName}`}
                        onClick={() => setSelectedLineId(line.id)}
                        className="rounded border bg-white px-1 py-0.5 text-[10px] font-medium leading-none"
                        style={{ borderColor: strokeFor(line), color: strokeFor(line) }}
                      >
                        {flag ?? (
                          <span
                            className="inline-block w-1.5 h-1.5 rounded-full align-middle"
                            style={{ background: strokeFor(line) }}
                          />
                        )}
                      </button>
                    )}
                  </div>
                );
              })}
            </div>
          </div>

          {deviceB ? (
            <PortColumn
              {...columnDataProps("target")}
              onPortMouseDown={targetMouseDown}
              onPortActivate={targetActivate}
              registerRowRef={targetRegisterRowRef}
              onScroll={targetScroll}
            />
          ) : (
            <EmptyColumn side="target" />
          )}
        </div>

        {reviewOpen && (
          <div
            data-testid="review-strip"
            className="bg-gray-50 border-t border-gray-200 max-h-[168px] overflow-y-auto"
          >
            {lines.length === 0 && (
              <p className="px-3 py-2 text-xs text-gray-400">No connections staged</p>
            )}
            {lines.map((line) => (
              <div
                key={line.id}
                className="flex items-center gap-2 px-3 py-1 text-xs border-b border-gray-100 last:border-b-0"
              >
                <span className="font-mono">{line.sourcePortName}</span>
                <span className="text-gray-400">to</span>
                <span className="font-mono">{line.targetPortName}</span>
                {line.duplicate && (
                  <span className="text-[10px] uppercase text-amber-700">duplicate</span>
                )}
                {line.error && <span className="text-[11px] text-red-600">{line.error}</span>}
                <button
                  type="button"
                  onClick={() => handleRemoveLine(line.id)}
                  aria-label={`Remove ${line.sourcePortName} to ${line.targetPortName}`}
                  className="ml-auto text-gray-400 hover:text-gray-600"
                >
                  &times;
                </button>
              </div>
            ))}
          </div>
        )}

        <div className="flex items-end gap-3 px-4 py-3 border-t border-gray-200">
          <div className="w-[180px]">
            <label
              htmlFor="multi-connect-type"
              className="block text-xs font-medium text-gray-700 mb-1"
            >
              Connection type
            </label>
            <input
              id="multi-connect-type"
              type="text"
              value={connectionType}
              onChange={(e) => setConnectionType(e.target.value)}
              className="w-full border border-gray-300 rounded-md px-2 py-1.5 text-xs focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>
          <div className="flex-1 min-w-0">
            <label
              htmlFor="multi-connect-notes"
              className="block text-xs font-medium text-gray-700 mb-1"
            >
              Notes
            </label>
            <input
              id="multi-connect-notes"
              type="text"
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              className="w-full border border-gray-300 rounded-md px-2 py-1.5 text-xs focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>
          <p className="text-[11px] text-gray-500 pb-2">Applied to every staged connection</p>
        </div>

        <div className="flex items-center gap-3 px-4 py-3 border-t border-gray-200">
          <div className="flex-1 min-w-0">
            <p className="text-xs text-gray-500">
              Free ports: {freeCounts.source} on A, {freeCounts.target} on B
            </p>
            {duplicateCount > 0 && (
              <p data-testid="duplicate-notice" className="text-[11px] text-amber-700 mt-0.5">
                {duplicateCount === 1
                  ? "1 staged pair duplicates an existing connection"
                  : `${duplicateCount} staged pairs duplicate an existing connection`}
              </p>
            )}
            {summary && (
              <p data-testid="submit-summary" className="text-xs text-red-600 mt-0.5">
                {summary}
              </p>
            )}
            {errorMessage && <p className="text-xs text-red-600 mt-0.5">{errorMessage}</p>}
          </div>
          <Button variant="ghost" size="sm" onClick={() => setReviewOpen((v) => !v)}>
            Review ({lines.length})
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={handleConnectOneToOne}
            disabled={interactionDisabled}
          >
            Connect 1:1 in order
          </Button>
          <Button variant="secondary" size="sm" onClick={onCancel}>
            Cancel
          </Button>
          <Button
            variant="primary"
            size="sm"
            onClick={handleConfirm}
            disabled={lines.length === 0 || interactionDisabled || !deviceA || !deviceB}
          >
            {confirmLabel}
          </Button>
        </div>
      </div>
    </Modal>
  );
}
