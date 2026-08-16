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
import { genId } from "@/lib/id";

// Shared by addEnrichedEdge and addEnrichedEdges (issue #517 review round 3
// item 9): every enriched edge, whether created one at a time or in a batch,
// gets its own generated id and goes through this exact same shape. Never
// addEdge, whose connectionExists dedupe guard silently refuses a second
// edge on identical source/target/handles.
function buildEnrichedEdge(connection: Connection, data: LayerEdgeData): Edge<LayerEdgeData> {
  return {
    id: genId(),
    source: connection.source,
    target: connection.target,
    sourceHandle: connection.sourceHandle ?? null,
    targetHandle: connection.targetHandle ?? null,
    type: "layerEdge",
    data,
  };
}

interface TopologyState {
  nodes: Node<CanvasNodeData>[];
  edges: Edge<LayerEdgeData>[];
  selectedEdgeLayer: EdgeLayerType;

  onNodesChange: (changes: NodeChange[]) => void;
  onEdgesChange: (changes: EdgeChange[]) => void;
  onConnect: (connection: Connection) => void;
  addEnrichedEdge: (connection: Connection, data: LayerEdgeData) => void;
  addEnrichedEdges: (items: Array<{ connection: Connection; data: LayerEdgeData }>) => void;
  addDeviceNode: (node: Node<CanvasNodeData>) => void;
  // Explicit node-delete safety net (issue #517 review item 3): removes every
  // store edge incident to any of the given node ids, independent of
  // whatever React Flow's own incident-edge computation does against the
  // (possibly bundled) render view.
  removeEdgesIncidentToNodes: (nodeIds: string[]) => void;
  // Single-edge removal (issue #517 review item 2): the bundled-edge popover's
  // per-member delete control removes just that one connection, distinct
  // from the bundle-level Delete which removes every member.
  removeEdge: (edgeId: string) => void;
  setDynamicPlaceholderCount: (nodeId: string, count: number) => void;
  setSelectedEdgeLayer: (layer: EdgeLayerType) => void;
  // One write path (issue #517 review round 3 item 12.5): the previous
  // singular updateEdgePathStatus (one store commit per changed edge) was
  // deleted once every caller moved to the batched variant below, so there
  // is exactly one way to write path status, not two that could drift.
  updateEdgePathStatuses: (
    updates: Map<string, { pathValid: boolean | null; hopCount?: number }>,
  ) => void;
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

  // addEnrichedEdge is a one-item wrapper over the same append path as
  // addEnrichedEdges (issue #517 review round 3 item 9), not addEdge: addEdge
  // carries a connectionExists dedupe guard (same source/target/handles
  // refuses a second edge), which silently dropped a second AI-proposed edge
  // between the same role pair. Every edge creation now shares one id
  // generator (genId) and one append path, with no dedupe anywhere.
  addEnrichedEdge: (connection, data) =>
    set((state) => ({ edges: [...state.edges, buildEnrichedEdge(connection, data)] })),

  // Bulk, single-commit add for the wiring dialog: every confirmed line
  // becomes its own edge object with its own generated id (never a shared
  // or derived id), exactly like addEnrichedEdge produces for one edge today.
  addEnrichedEdges: (items) =>
    set((state) => ({
      edges: [...state.edges, ...items.map(({ connection, data }) => buildEnrichedEdge(connection, data))],
    })),

  addDeviceNode: (node) =>
    set((state) => ({ nodes: [...state.nodes, node] })),

  removeEdgesIncidentToNodes: (nodeIds) =>
    set((state) => {
      if (nodeIds.length === 0) return state;
      const idSet = new Set(nodeIds);
      return {
        edges: state.edges.filter((e) => !idSet.has(e.source) && !idSet.has(e.target)),
      };
    }),

  removeEdge: (edgeId) =>
    set((state) => ({ edges: state.edges.filter((e) => e.id !== edgeId) })),

  setDynamicPlaceholderCount: (nodeId, count) =>
    set((state) => ({
      nodes: state.nodes.map((n) =>
        n.id === nodeId && n.type === "dynamicPlaceholderNode"
          ? { ...n, data: { ...n.data, count } }
          : n
      ),
    })),

  setSelectedEdgeLayer: (layer) => set({ selectedEdgeLayer: layer }),

  updateEdgePathStatuses: (updates) =>
    set((state) => {
      if (updates.size === 0) return state;
      return {
        edges: state.edges.map((e) => {
          const update = updates.get(e.id);
          if (!update) return e;
          return { ...e, data: { ...e.data!, pathValid: update.pathValid, pathHopCount: update.hopCount } };
        }) as Edge<LayerEdgeData>[],
      };
    }),

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
