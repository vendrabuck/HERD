import { useMemo, useState } from "react";
import { Modal } from "@/components/ui/Modal";
import { usePorts } from "@/api/ports";
import { useDeviceConnections } from "@/api/connections";
import type { EdgeLayerType, LayerEdgeData } from "@/types/topology.types";

const LAYER_OPTIONS: EdgeLayerType[] = ["L1", "L2", "L3"];

const LAYER_COLORS: Record<EdgeLayerType, string> = {
  L1: "bg-gray-500 text-white",
  L2: "bg-blue-500 text-white",
  L3: "bg-green-500 text-white",
};

interface ConnectionModalProps {
  open: boolean;
  sourceDeviceId: string;
  sourceDeviceName: string;
  targetDeviceId: string;
  targetDeviceName: string;
  defaultLayer: EdgeLayerType;
  onConfirm: (data: LayerEdgeData) => void;
  onCancel: () => void;
}

export function ConnectionModal({
  open,
  sourceDeviceId,
  sourceDeviceName,
  targetDeviceId,
  targetDeviceName,
  defaultLayer,
  onConfirm,
  onCancel,
}: ConnectionModalProps) {
  const [layer, setLayer] = useState<EdgeLayerType>(defaultLayer);
  const [sourcePortId, setSourcePortId] = useState("");
  const [targetPortId, setTargetPortId] = useState("");

  const { data: sourcePorts = [], isLoading: sourceLoading } = usePorts(sourceDeviceId);
  const { data: targetPorts = [], isLoading: targetLoading } = usePorts(targetDeviceId);
  const { data: sourceConns = [], isLoading: sourceConnsLoading } = useDeviceConnections(sourceDeviceId);
  const { data: targetConns = [], isLoading: targetConnsLoading } = useDeviceConnections(targetDeviceId);

  const sourceCabledPorts = useMemo(() => {
    const names = new Set<string>();
    for (const conn of sourceConns) {
      if (conn.device_a_id === sourceDeviceId) names.add(conn.port_a);
      if (conn.device_b_id === sourceDeviceId) names.add(conn.port_b);
    }
    return names;
  }, [sourceConns, sourceDeviceId]);

  const targetCabledPorts = useMemo(() => {
    const names = new Set<string>();
    for (const conn of targetConns) {
      if (conn.device_a_id === targetDeviceId) names.add(conn.port_a);
      if (conn.device_b_id === targetDeviceId) names.add(conn.port_b);
    }
    return names;
  }, [targetConns, targetDeviceId]);

  const selectedSourcePort = sourcePorts.find((p) => p.id === sourcePortId);
  const selectedTargetPort = targetPorts.find((p) => p.id === targetPortId);
  const sourcePortCabled = selectedSourcePort ? sourceCabledPorts.has(selectedSourcePort.name) : true;
  const targetPortCabled = selectedTargetPort ? targetCabledPorts.has(selectedTargetPort.name) : true;

  const connectionsLoading = sourceConnsLoading || targetConnsLoading;
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
    <Modal open={open} onClose={onCancel} title="New Connection">
      <div className="flex flex-col gap-5">
        {/* Layer selector */}
        <div>
          <label className="block text-sm font-medium text-gray-700 mb-2">Layer</label>
          <div className="flex gap-1">
            {LAYER_OPTIONS.map((l) => (
              <button
                key={l}
                onClick={() => setLayer(l)}
                className={`px-4 py-1.5 rounded text-sm font-medium transition-colors ${
                  layer === l ? LAYER_COLORS[l] : "bg-gray-100 text-gray-700 hover:bg-gray-200"
                }`}
              >
                {l}
              </button>
            ))}
          </div>
        </div>

        {/* Source port */}
        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1">
            Source port ({sourceDeviceName})
          </label>
          {sourceLoading ? (
            <p className="text-sm text-gray-400">Loading ports...</p>
          ) : sourcePorts.length === 0 ? (
            <select disabled className="w-full border rounded px-3 py-2 text-sm bg-gray-50 text-gray-400">
              <option>No ports configured</option>
            </select>
          ) : (
            <select
              value={sourcePortId}
              onChange={(e) => setSourcePortId(e.target.value)}
              className="w-full border rounded px-3 py-2 text-sm"
            >
              <option value="">Select a port</option>
              {sourcePorts.map((port) => {
                const cabled = sourceCabledPorts.has(port.name);
                return (
                  <option key={port.id} value={port.id} className={cabled ? "" : "text-gray-400"}>
                    {port.name}{cabled ? "" : " (no cable)"}
                  </option>
                );
              })}
            </select>
          )}
          {sourcePortId && !sourcePortCabled && (
            <p className="text-xs text-red-500 mt-1">This port has no physical cable connected</p>
          )}
        </div>

        {/* Target port */}
        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1">
            Target port ({targetDeviceName})
          </label>
          {targetLoading ? (
            <p className="text-sm text-gray-400">Loading ports...</p>
          ) : targetPorts.length === 0 ? (
            <select disabled className="w-full border rounded px-3 py-2 text-sm bg-gray-50 text-gray-400">
              <option>No ports configured</option>
            </select>
          ) : (
            <select
              value={targetPortId}
              onChange={(e) => setTargetPortId(e.target.value)}
              className="w-full border rounded px-3 py-2 text-sm"
            >
              <option value="">Select a port</option>
              {targetPorts.map((port) => {
                const cabled = targetCabledPorts.has(port.name);
                return (
                  <option key={port.id} value={port.id} className={cabled ? "" : "text-gray-400"}>
                    {port.name}{cabled ? "" : " (no cable)"}
                  </option>
                );
              })}
            </select>
          )}
          {targetPortId && !targetPortCabled && (
            <p className="text-xs text-red-500 mt-1">This port has no physical cable connected</p>
          )}
        </div>

        {/* Actions */}
        <div className="flex justify-end gap-2 pt-2">
          <button
            onClick={onCancel}
            className="px-4 py-2 text-sm text-gray-700 bg-gray-100 rounded hover:bg-gray-200"
          >
            Cancel
          </button>
          <button
            onClick={handleConfirm}
            disabled={!canConnect}
            className="px-4 py-2 text-sm text-white bg-blue-600 rounded hover:bg-blue-700 disabled:opacity-40 disabled:cursor-not-allowed"
          >
            Connect
          </button>
        </div>
      </div>
    </Modal>
  );
}
