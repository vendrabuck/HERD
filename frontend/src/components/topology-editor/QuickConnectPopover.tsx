import { useState } from "react";
import { Modal } from "@/components/ui/Modal";
import { Button } from "@/components/ui/Button";
import { usePortAvailability } from "./wiring/usePortAvailability";
import { EMPTY_ID_SET } from "./wiring/portAvailability";
import { LayerSegmentedControl } from "./wiring/LayerSegmentedControl";
import type { EdgeLayerType, LayerEdgeData } from "@/types/topology.types";

export interface QuickConnectPopoverProps {
  open: boolean;
  sourceDeviceId: string;
  sourceDeviceName: string;
  targetDeviceId: string;
  targetDeviceName: string;
  defaultLayer: EdgeLayerType;
  // Ports already wired to the counterpart device by an EXISTING canvas edge
  // (issue #517 review item 8): disabled here exactly like the dialog's
  // WIRED rows, so the same port pair cannot be drawn twice.
  existingWiredSourcePortIds?: ReadonlySet<string>;
  existingWiredTargetPortIds?: ReadonlySet<string>;
  onConfirm: (data: LayerEdgeData) => void;
  onCancel: () => void;
  onEscalate: () => void;
}

/**
 * Compact quick path (issue #517 addendum decision 1): the fast single-line
 * flow that survives from the original ConnectionModal, plus an escalation
 * link into the full WiringDialog for the same device pair.
 *
 * Cabling semantics (review item 1, restored to the OLD ConnectionModal
 * behavior exactly): a port with ANY registered physical connection is
 * selectable and marks the edge portsCabled true; an uncabled port stays
 * selectable too, with the "(no cable)" suffix, and lands portsCabled false
 * (the existing soft canvas warning). Nothing is blocked by cabling state.
 */
export function QuickConnectPopover({
  open,
  sourceDeviceId,
  sourceDeviceName,
  targetDeviceId,
  targetDeviceName,
  defaultLayer,
  existingWiredSourcePortIds = EMPTY_ID_SET,
  existingWiredTargetPortIds = EMPTY_ID_SET,
  onConfirm,
  onCancel,
  onEscalate,
}: QuickConnectPopoverProps) {
  const [layer, setLayer] = useState<EdgeLayerType>(defaultLayer);
  const [sourcePortId, setSourcePortId] = useState("");
  const [targetPortId, setTargetPortId] = useState("");

  const { source, target, connectionsLoading } = usePortAvailability(sourceDeviceId, targetDeviceId);

  const selectedSourcePort = source.ports.find((p) => p.id === sourcePortId);
  const selectedTargetPort = target.ports.find((p) => p.id === targetPortId);
  const sourcePortCabled = selectedSourcePort ? source.cabled.has(selectedSourcePort.name) : false;
  const targetPortCabled = selectedTargetPort ? target.cabled.has(selectedTargetPort.name) : false;

  // Never compute selectability/portsCabled against a still-empty connections
  // list (issue #517 review item 6): while either side's cabling fetch is in
  // flight, the whole popover is inert.
  const canConnect = sourcePortId !== "" && targetPortId !== "" && !connectionsLoading;

  const handleConfirm = () => {
    onConfirm({
      layer,
      source_port_id: sourcePortId,
      source_port_name: selectedSourcePort?.name,
      target_port_id: targetPortId,
      target_port_name: selectedTargetPort?.name,
      portsCabled: sourcePortCabled && targetPortCabled,
    });
  };

  return (
    <Modal open={open} onClose={onCancel} title="New connection" className="max-w-sm">
      <div className="flex flex-col gap-4">
        <div>
          <label className="block text-sm font-medium text-gray-700 mb-2">Layer</label>
          <LayerSegmentedControl value={layer} onChange={setLayer} size="md" />
        </div>

        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1">
            Source port ({sourceDeviceName})
          </label>
          {source.isLoading ? (
            <p className="text-sm text-gray-400">Loading ports...</p>
          ) : source.ports.length === 0 ? (
            <select disabled className="w-full border rounded px-3 py-2 text-sm bg-gray-50 text-gray-400">
              <option>No ports configured</option>
            </select>
          ) : (
            <select
              value={sourcePortId}
              onChange={(e) => setSourcePortId(e.target.value)}
              disabled={connectionsLoading}
              className="w-full border rounded px-3 py-2 text-sm font-mono disabled:bg-gray-50 disabled:text-gray-400"
            >
              <option value="">Select a port</option>
              {source.ports.map((port) => {
                const alreadyWired = existingWiredSourcePortIds.has(port.id);
                const cabled = source.cabled.has(port.name);
                return (
                  <option key={port.id} value={port.id} disabled={alreadyWired}>
                    {port.name}
                    {alreadyWired ? " (already connected)" : cabled ? "" : " (no cable)"}
                  </option>
                );
              })}
            </select>
          )}
          {sourcePortId && !sourcePortCabled && (
            <p className="text-xs text-red-500 mt-1">This port has no physical cable connected</p>
          )}
        </div>

        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1">
            Target port ({targetDeviceName})
          </label>
          {target.isLoading ? (
            <p className="text-sm text-gray-400">Loading ports...</p>
          ) : target.ports.length === 0 ? (
            <select disabled className="w-full border rounded px-3 py-2 text-sm bg-gray-50 text-gray-400">
              <option>No ports configured</option>
            </select>
          ) : (
            <select
              value={targetPortId}
              onChange={(e) => setTargetPortId(e.target.value)}
              disabled={connectionsLoading}
              className="w-full border rounded px-3 py-2 text-sm font-mono disabled:bg-gray-50 disabled:text-gray-400"
            >
              <option value="">Select a port</option>
              {target.ports.map((port) => {
                const alreadyWired = existingWiredTargetPortIds.has(port.id);
                const cabled = target.cabled.has(port.name);
                return (
                  <option key={port.id} value={port.id} disabled={alreadyWired}>
                    {port.name}
                    {alreadyWired ? " (already connected)" : cabled ? "" : " (no cable)"}
                  </option>
                );
              })}
            </select>
          )}
          {targetPortId && !targetPortCabled && (
            <p className="text-xs text-red-500 mt-1">This port has no physical cable connected</p>
          )}
        </div>

        <div className="flex justify-between items-center pt-2">
          <Button variant="ghost" size="sm" onClick={onEscalate} className="px-0">
            Open wiring dialog
          </Button>
          <div className="flex gap-2">
            <Button variant="secondary" onClick={onCancel}>
              Cancel
            </Button>
            <Button variant="primary" onClick={handleConfirm} disabled={!canConnect}>
              Connect
            </Button>
          </div>
        </div>
      </div>
    </Modal>
  );
}
