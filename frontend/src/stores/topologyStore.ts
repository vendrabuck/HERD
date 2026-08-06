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
import type { CanvasData, CanvasNodeData, LayerEdgeData, EdgeLayerType } from "@/types/topology.types";

interface TopologyState {
  nodes: Node<CanvasNodeData>[];
  edges: Edge<LayerEdgeData>[];
  selectedEdgeLayer: EdgeLayerType;

  onNodesChange: (changes: NodeChange[]) => void;
  onEdgesChange: (changes: EdgeChange[]) => void;
  onConnect: (connection: Connection) => void;
  addEnrichedEdge: (connection: Connection, data: LayerEdgeData) => void;
  addDeviceNode: (node: Node<CanvasNodeData>) => void;
  setDynamicPlaceholderCount: (nodeId: string, count: number) => void;
  setSelectedEdgeLayer: (layer: EdgeLayerType) => void;
  updateEdgePathStatus: (edgeId: string, pathValid: boolean | null, hopCount?: number) => void;
  clearTopology: () => void;
  loadCanvas: (data: CanvasData) => void;
  acceptProposalNodes: () => void;
  rejectProposalNodes: () => void;
}

export const useTopologyStore = create<TopologyState>()((set) => ({
  nodes: [],
  edges: [],
  selectedEdgeLayer: "L2",

  onNodesChange: (changes) =>
    set((state) => ({ nodes: applyNodeChanges(changes, state.nodes) as Node<CanvasNodeData>[] })),

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

  addEnrichedEdge: (connection, data) =>
    set((state) => ({
      edges: addEdge(
        {
          ...connection,
          type: "layerEdge",
          data,
        },
        state.edges
      ) as Edge<LayerEdgeData>[],
    })),

  addDeviceNode: (node) =>
    set((state) => ({ nodes: [...state.nodes, node] })),

  setDynamicPlaceholderCount: (nodeId, count) =>
    set((state) => ({
      nodes: state.nodes.map((n) =>
        n.id === nodeId && n.type === "dynamicPlaceholderNode"
          ? { ...n, data: { ...n.data, count } }
          : n
      ),
    })),

  setSelectedEdgeLayer: (layer) => set({ selectedEdgeLayer: layer }),

  updateEdgePathStatus: (edgeId, pathValid, hopCount) =>
    set((state) => ({
      edges: state.edges.map((e) =>
        e.id === edgeId
          ? { ...e, data: { ...e.data!, pathValid, pathHopCount: hopCount } }
          : e
      ) as Edge<LayerEdgeData>[],
    })),

  clearTopology: () => set({ nodes: [], edges: [], selectedEdgeLayer: "L2" }),

  loadCanvas: (data) =>
    set({
      nodes: data.nodes ?? [],
      edges: data.edges ?? [],
      selectedEdgeLayer: data.selectedEdgeLayer ?? "L2",
    }),

  acceptProposalNodes: () =>
    set((state) => ({
      nodes: state.nodes.map((n) =>
        n.data?.isProposal
          ? { ...n, data: { ...n.data, isProposal: false } }
          : n
      ),
      edges: state.edges.map((e) =>
        e.data?.isProposal
          ? { ...e, data: { ...e.data, isProposal: false } }
          : e
      ),
    })),

  rejectProposalNodes: () =>
    set((state) => ({
      nodes: state.nodes.filter((n) => !n.data?.isProposal),
      edges: state.edges.filter((e) => !e.data?.isProposal),
    })),
}));
