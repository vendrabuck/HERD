import { useCallback, useMemo, useState } from "react";
import { Network, Waypoints, Cloud, Cable } from "lucide-react";
import { Modal } from "@/components/ui/Modal";
import { Button } from "@/components/ui/Button";
import { usePortAvailability } from "./wiring/usePortAvailability";
import { PortColumn, type PortRowVisual, type Side } from "./wiring/PortColumn";
import { filterPorts } from "./wiring/filterPorts";
import type { TopologyType } from "@/types/device.types";
import type { NetworkElementType } from "@/types/topology.types";
import type { Port } from "@/types/port.types";

const ELEMENT_ICONS: Record<NetworkElementType, typeof Network> = {
  vlan_segment: Network,
  subnet: Waypoints,
  external_cloud: Cloud,
  patch_trunk: Cable,
};

export interface ElementAttachSelection {
  portId: string;
  portName: string;
}

export interface ElementAttachDialogProps {
  open: boolean;
  deviceId: string;
  deviceName: string;
  deviceTopologyType: TopologyType;
  elementId: string;
  elementLabel: string;
  elementType: NetworkElementType;
  // Ports already used by an existing canvas edge touching this device
  // (WiringDialog's cross-session duplicate-prevention rule, ADR 0012
  // "Editing surface": a port already wired on the canvas to ANY node,
  // device or element, is unavailable here too).
  existingWiredPortIds?: ReadonlySet<string>;
  onConfirm: (selections: ElementAttachSelection[]) => void;
  onCancel: () => void;
}

/**
 * Single device-side port picker that attaches N ports to one network
 * element (ADR 0012 "Editing surface"). NOT WiringDialog: an element has no
 * ports and no second device-shaped side, so WiringDialogProps'
 * targetDeviceId/targetTopologyType and its two-column drag/click wiring
 * geometry do not apply. This reuses the same PortColumn/filterPorts/
 * usePortAvailability/portAvailability primitives directly instead.
 *
 * usePortAvailability is called with the device id on BOTH sides (the ADR's
 * "either way" choice): it is the one-line reuse of the existing hook with no
 * new one-sided variant to introduce and test, and the unused `target` half
 * costs nothing extra (TanStack Query dedupes the identical query key against
 * `source`'s own fetch of the same device). Only `source` is read below.
 *
 * Selection here is MULTI-select (a plain toggle per port row), unlike
 * WiringDialog's arm-then-pair-with-the-other-column model: PortColumn's own
 * click affordance is reused for the row chrome, but onPortActivate toggles
 * membership in a local Set instead of arming a cross-column pair.
 */
export function ElementAttachDialog({
  open,
  deviceId,
  deviceName,
  deviceTopologyType,
  elementLabel,
  elementType,
  existingWiredPortIds,
  onConfirm,
  onCancel,
}: ElementAttachDialogProps) {
  const { source, connectionsLoading } = usePortAvailability(deviceId, deviceId);
  const [selected, setSelected] = useState<Map<string, string>>(new Map());
  const [filter, setFilter] = useState("");

  const filteredPorts = useMemo(() => filterPorts(source.ports, filter), [source.ports, filter]);

  const visuals = useMemo(() => {
    const wiredIds = existingWiredPortIds ?? new Set<string>();
    const map = new Map<string, PortRowVisual>();
    for (const port of source.ports) {
      const cabled = source.cabled.has(port.name);
      if (selected.has(port.id)) {
        map.set(port.id, { status: "session-wired", cabled });
      } else if (wiredIds.has(port.id)) {
        map.set(port.id, { status: "canvas-wired", cabled });
      } else {
        map.set(port.id, { status: "free", cabled });
      }
    }
    return map;
  }, [source.ports, source.cabled, selected, existingWiredPortIds]);

  // Multi-select toggle (ADR 0012 "Editing surface"): unlike WiringDialog's
  // arm-then-pair-with-the-other-column model, a click here just flips one
  // port's membership in the selected Set. A port that is already
  // canvas-wired (existingWiredPortIds, or session-wired i.e. already
  // selected and re-clicked to deselect) is the only status transition this
  // needs to gate: session-wired re-toggles off, free/canvas-wired never
  // toggle a wired port on.
  const handleToggle = useCallback(
    (port: Port) => {
      const visual = visuals.get(port.id);
      if (visual?.status === "canvas-wired") return;
      setSelected((prev) => {
        const next = new Map(prev);
        if (next.has(port.id)) {
          next.delete(port.id);
        } else {
          next.set(port.id, port.name);
        }
        return next;
      });
    },
    [visuals],
  );

  // PortColumn's row wires plain-click activation through onMouseDown (see
  // WiringDialog's own global press-tracking); onPortActivate itself only
  // fires on keyboard Enter/Space. This dialog has no drag geometry to
  // arbitrate, so mousedown toggles directly.
  const handleMouseDown = useCallback(
    (port: Port, _e: React.MouseEvent) => {
      if (connectionsLoading) return;
      handleToggle(port);
    },
    [connectionsLoading, handleToggle],
  );

  const noopRegisterRowRef = useCallback(() => {}, []);
  const noopScroll = useCallback(() => {}, []);

  const handleConfirm = useCallback(() => {
    if (selected.size === 0 || connectionsLoading) return;
    onConfirm(Array.from(selected, ([portId, portName]) => ({ portId, portName })));
  }, [selected, connectionsLoading, onConfirm]);

  const Icon = ELEMENT_ICONS[elementType];
  const side: Side = "source";

  const confirmLabel =
    selected.size === 0
      ? "Attach"
      : selected.size === 1
        ? "Attach 1 port"
        : `Attach ${selected.size} ports`;

  return (
    <Modal
      open={open}
      onClose={onCancel}
      title={`Attach ${deviceName} to ${elementLabel}`}
      className="max-w-[640px]"
      bodyClassName="p-0"
    >
      <div className="flex flex-col max-h-[calc(96vh-56px)]">
        <div className="flex flex-1 min-h-0">
          <PortColumn
            side={side}
            deviceName={deviceName}
            topologyType={deviceTopologyType}
            ports={filteredPorts}
            totalCount={source.ports.length}
            isLoading={source.isLoading}
            visuals={visuals}
            cabledCount={source.cabled.size}
            filter={filter}
            onFilterChange={setFilter}
            armedPortId={null}
            highlightFree={false}
            interactionDisabled={connectionsLoading}
            onPortMouseDown={handleMouseDown}
            onPortActivate={handleToggle}
            registerRowRef={noopRegisterRowRef}
            onScroll={noopScroll}
          />

          {/* The element side: a static target card, not a port list (ADR
              0012 "Editing surface"). Elements have no ports to select. */}
          <div className="flex-1 flex flex-col items-center justify-center gap-2 border-x border-gray-200 min-w-[240px] bg-gray-50 p-4">
            <span className="inline-flex items-center justify-center w-10 h-10 rounded bg-gray-200 text-gray-600">
              <Icon className="w-5 h-5" />
            </span>
            <span className="text-sm font-semibold text-gray-900 text-center">{elementLabel}</span>
            <span className="text-[11px] text-gray-500 text-center">
              {selected.size} port{selected.size === 1 ? "" : "s"} selected
            </span>
          </div>
        </div>

        <div className="flex items-center gap-3 px-4 py-3 border-t border-gray-200">
          <p className="flex-1 min-w-0 text-xs text-gray-500">
            Select one or more device ports, then Attach. Each selected port becomes one
            attachment edge.
          </p>
          <Button variant="secondary" size="sm" onClick={onCancel}>
            Cancel
          </Button>
          <Button
            variant="primary"
            size="sm"
            onClick={handleConfirm}
            disabled={selected.size === 0 || connectionsLoading}
          >
            {confirmLabel}
          </Button>
        </div>
      </div>
    </Modal>
  );
}
