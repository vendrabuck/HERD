import { create } from "zustand";
import {
  addEdge,
  applyEdgeChanges,
  applyNodeChanges,
  type Connection,
  type Edge,
  type EdgeChange,
  type Node,
  type NodeChange,
} from "@xyflow/react";
import type { CanvasData, DeviceNodeData, LayerEdgeData, EdgeLayerType } from "@/types/topology.types";

interface TopologyState {
  nodes: Node<DeviceNodeData>[];
  edges: Edge<LayerEdgeData>[];
  selectedEdgeLayer: EdgeLayerType;

  onNodesChange: (changes: NodeChange[]) => void;
  onEdgesChange: (changes: EdgeChange[]) => void;
  onConnect: (connection: Connection) => void;
  addDeviceNode: (node: Node<DeviceNodeData>) => void;
  setSelectedEdgeLayer: (layer: EdgeLayerType) => void;
  clearTopology: () => void;
  loadCanvas: (data: CanvasData) => void;
}

export const useTopologyStore = create<TopologyState>()((set) => ({
  nodes: [],
  edges: [],
  selectedEdgeLayer: "L2",

  onNodesChange: (changes) =>
    set((state) => ({ nodes: applyNodeChanges(changes, state.nodes) as Node<DeviceNodeData>[] })),

  onEdgesChange: (changes) =>
    set((state) => ({ edges: applyEdgeChanges(changes, state.edges) as Edge<LayerEdgeData>[] })),

  onConnect: (connection) =>
    set((state) => ({
      edges: addEdge(
        {
          ...connection,
          type: "layerEdge",
          data: { layer: state.selectedEdgeLayer },
        },
        state.edges
      ) as Edge<LayerEdgeData>[],
    })),

  addDeviceNode: (node) =>
    set((state) => ({ nodes: [...state.nodes, node] })),

  setSelectedEdgeLayer: (layer) => set({ selectedEdgeLayer: layer }),

  clearTopology: () => set({ nodes: [], edges: [], selectedEdgeLayer: "L2" }),

  loadCanvas: (data) =>
    set({
      nodes: data.nodes ?? [],
      edges: data.edges ?? [],
      selectedEdgeLayer: data.selectedEdgeLayer ?? "L2",
    }),
}));
