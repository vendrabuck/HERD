import { useCallback, useRef, useState } from "react";
import {
  ReactFlow,
  Background,
  Controls,
  MiniMap,
  type Connection,
  type Node,
  type OnConnect,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import toast from "react-hot-toast";

import { useTopologyStore } from "@/stores/topologyStore";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { CreateReservationModal } from "@/components/reservations/CreateReservationModal";
import { DeviceNode } from "./nodes/DeviceNode";
import { LayerEdge } from "./edges/LayerEdge";
import type { Device } from "@/types/device.types";
import type { DeviceNodeData } from "@/types/topology.types";
import type { EdgeLayerType } from "@/types/topology.types";

const nodeTypes = { deviceNode: DeviceNode };
const edgeTypes = { layerEdge: LayerEdge };

const LAYER_OPTIONS: EdgeLayerType[] = ["L1", "L2", "L3"];

const LAYER_DESCRIPTIONS: Record<EdgeLayerType, string> = {
  L1: "Physical / fiber",
  L2: "Ethernet / VLAN",
  L3: "IP routing",
};

export function TopologyEditor() {
  const {
    nodes,
    edges,
    onNodesChange,
    onEdgesChange,
    onConnect,
    addDeviceNode,
    selectedEdgeLayer,
    setSelectedEdgeLayer,
    clearTopology,
  } = useTopologyStore();

  const reactFlowWrapper = useRef<HTMLDivElement>(null);
  const [showClearConfirm, setShowClearConfirm] = useState(false);
  const [showReserveModal, setShowReserveModal] = useState(false);

  const selectedDeviceIds = nodes
    .filter((n) => n.selected)
    .map((n) => (n.data as DeviceNodeData).device.id);

  // Block connections between PHYSICAL and CLOUD nodes
  const isValidConnection = useCallback(
    (connection: Connection | { source: string; target: string }): boolean => {
      const sourceNode = nodes.find((n) => n.id === connection.source);
      const targetNode = nodes.find((n) => n.id === connection.target);

      if (!sourceNode || !targetNode) return false;

      const sourceType = (sourceNode.data as DeviceNodeData).device.topology_type;
      const targetType = (targetNode.data as DeviceNodeData).device.topology_type;

      if (sourceType !== targetType) {
        toast.error(
          `Cannot connect ${sourceType} and ${targetType} devices: topology types must match`,
          { id: "topology-mismatch" }
        );
        return false;
      }
      return true;
    },
    [nodes]
  );

  // Handle drop from EquipmentBrowser
  const onDrop = useCallback(
    (event: React.DragEvent<HTMLDivElement>) => {
      event.preventDefault();
      const deviceJson = event.dataTransfer.getData("application/herd-device");
      if (!deviceJson) return;

      const device: Device = JSON.parse(deviceJson);

      const bounds = reactFlowWrapper.current?.getBoundingClientRect();
      if (!bounds) return;

      const position = {
        x: event.clientX - bounds.left - 70,
        y: event.clientY - bounds.top - 60,
      };

      const newNode: Node<DeviceNodeData> = {
        id: self.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2, 11)}`,
        type: "deviceNode",
        position,
        data: {
          device,
          label: device.name,
          topologyType: device.topology_type,
        },
      };

      addDeviceNode(newNode);
    },
    [addDeviceNode]
  );

  const onDragOver = useCallback((event: React.DragEvent) => {
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
  }, []);

  const handleConnect: OnConnect = useCallback(
    (connection) => {
      onConnect(connection);
    },
    [onConnect]
  );

  return (
    <div className="flex flex-col h-full">
      {/* Toolbar */}
      <div className="flex items-center gap-3 px-4 py-2 bg-white border-b border-gray-200">
        <span className="text-sm font-medium text-gray-600">Edge layer:</span>
        <div className="flex gap-1">
          {LAYER_OPTIONS.map((layer) => (
            <button
              key={layer}
              onClick={() => setSelectedEdgeLayer(layer)}
              title={LAYER_DESCRIPTIONS[layer]}
              aria-label={`${layer}: ${LAYER_DESCRIPTIONS[layer]}`}
              aria-pressed={selectedEdgeLayer === layer}
              className={`px-3 py-1 rounded text-sm font-medium transition-colors ${
                selectedEdgeLayer === layer
                  ? "bg-blue-600 text-white"
                  : "bg-gray-100 text-gray-700 hover:bg-gray-200"
              }`}
            >
              {layer}
            </button>
          ))}
        </div>
        <div className="ml-auto flex items-center gap-2">
          <button
            onClick={() => setShowReserveModal(true)}
            disabled={selectedDeviceIds.length === 0}
            className="text-sm text-blue-600 hover:text-blue-800 px-2 py-1 rounded hover:bg-blue-50 disabled:opacity-40 disabled:cursor-not-allowed"
          >
            Reserve Selected ({selectedDeviceIds.length})
          </button>
          <button
            onClick={() => setShowClearConfirm(true)}
            className="text-sm text-red-600 hover:text-red-800 px-2 py-1 rounded hover:bg-red-50"
          >
            Clear canvas
          </button>
        </div>
      </div>

      {/* React Flow canvas */}
      <div ref={reactFlowWrapper} className="flex-1" onDrop={onDrop} onDragOver={onDragOver}>
        <ReactFlow
          nodes={nodes}
          edges={edges}
          onNodesChange={onNodesChange}
          onEdgesChange={onEdgesChange}
          onConnect={handleConnect}
          isValidConnection={isValidConnection}
          nodeTypes={nodeTypes}
          edgeTypes={edgeTypes}
          fitView
          deleteKeyCode="Delete"
        >
          <Background gap={16} size={1} color="#e5e7eb" />
          <Controls />
          <MiniMap
            nodeColor={(node) => {
              const data = node.data as DeviceNodeData;
              return data?.device?.topology_type === "CLOUD" ? "#a855f7" : "#3b82f6";
            }}
          />
        </ReactFlow>
      </div>

      <ConfirmDialog
        open={showClearConfirm}
        title="Clear canvas?"
        description="This will remove all devices and connections from the canvas."
        confirmLabel="Clear"
        destructive
        onConfirm={() => {
          clearTopology();
          setShowClearConfirm(false);
        }}
        onCancel={() => setShowClearConfirm(false)}
      />

      {showReserveModal && (
        <CreateReservationModal
          open={showReserveModal}
          deviceIds={selectedDeviceIds}
          onClose={() => setShowReserveModal(false)}
        />
      )}
    </div>
  );
}
