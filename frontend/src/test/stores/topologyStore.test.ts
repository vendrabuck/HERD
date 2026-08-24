import { useTopologyStore } from "@/stores/topologyStore";
import type { Node, Edge } from "@xyflow/react";
import type { DeviceNodeData, LayerEdgeData, CanvasData } from "@/types/topology.types";

const makeNode = (id: string): Node<DeviceNodeData> =>
  ({
    id,
    type: "deviceNode",
    position: { x: 0, y: 0 },
    data: {
      device: { id, name: `device-${id}` },
      label: `device-${id}`,
      topologyType: "PHYSICAL",
    },
  }) as Node<DeviceNodeData>;

const makeEdge = (id: string, source: string, target: string): Edge<LayerEdgeData> =>
  ({
    id,
    source,
    target,
    type: "layerEdge",
    data: { layer: "L2" },
  }) as Edge<LayerEdgeData>;

describe("topologyStore", () => {
  beforeEach(() => {
    useTopologyStore.getState().clearTopology();
  });

  it("initializes with empty nodes and edges", () => {
    const state = useTopologyStore.getState();
    expect(state.nodes).toEqual([]);
    expect(state.edges).toEqual([]);
    expect(state.selectedEdgeLayer).toBe("L2");
  });

  it("addDeviceNode adds a node", () => {
    const node = makeNode("n1");
    useTopologyStore.getState().addDeviceNode(node);
    const state = useTopologyStore.getState();
    expect(state.nodes).toHaveLength(1);
    expect(state.nodes[0].id).toBe("n1");
  });

  it("clearTopology resets state", () => {
    useTopologyStore.getState().addDeviceNode(makeNode("n1"));
    useTopologyStore.getState().setSelectedEdgeLayer("L3");
    useTopologyStore.getState().clearTopology();
    const state = useTopologyStore.getState();
    expect(state.nodes).toEqual([]);
    expect(state.edges).toEqual([]);
    expect(state.selectedEdgeLayer).toBe("L2");
  });

  it("setSelectedEdgeLayer updates layer", () => {
    useTopologyStore.getState().setSelectedEdgeLayer("L1");
    expect(useTopologyStore.getState().selectedEdgeLayer).toBe("L1");
  });

  it("onConnect creates an edge with current layer", () => {
    useTopologyStore.getState().addDeviceNode(makeNode("a"));
    useTopologyStore.getState().addDeviceNode(makeNode("b"));
    useTopologyStore.getState().setSelectedEdgeLayer("L3");
    useTopologyStore.getState().onConnect({ source: "a", target: "b", sourceHandle: null, targetHandle: null });
    const edges = useTopologyStore.getState().edges;
    expect(edges).toHaveLength(1);
    expect(edges[0].source).toBe("a");
    expect(edges[0].target).toBe("b");
    expect(edges[0].data?.layer).toBe("L3");
  });

  it("loadCanvas sets nodes and edges from data", () => {
    const canvasData: CanvasData = {
      nodes: [makeNode("x")],
      edges: [makeEdge("e1", "x", "x")],
      selectedEdgeLayer: "L1",
    };
    useTopologyStore.getState().loadCanvas(canvasData);
    const state = useTopologyStore.getState();
    expect(state.nodes).toHaveLength(1);
    expect(state.edges).toHaveLength(1);
    expect(state.selectedEdgeLayer).toBe("L1");
  });

  it("loadCanvas falls back to empty/L2 when fields are missing", () => {
    // CanvasData with absent nodes/edges/layer exercises the ?? defaults.
    useTopologyStore.getState().loadCanvas({} as CanvasData);
    const state = useTopologyStore.getState();
    expect(state.nodes).toEqual([]);
    expect(state.edges).toEqual([]);
    expect(state.selectedEdgeLayer).toBe("L2");
  });

  it("onNodesChange applies a position change", () => {
    useTopologyStore.getState().addDeviceNode(makeNode("a"));
    useTopologyStore.getState().onNodesChange([
      { id: "a", type: "position", position: { x: 42, y: 99 }, dragging: false },
    ]);
    const node = useTopologyStore.getState().nodes[0];
    expect(node.position).toEqual({ x: 42, y: 99 });
  });

  it("onNodesChange applies a remove change", () => {
    useTopologyStore.getState().addDeviceNode(makeNode("a"));
    useTopologyStore.getState().addDeviceNode(makeNode("b"));
    useTopologyStore.getState().onNodesChange([{ id: "a", type: "remove" }]);
    const ids = useTopologyStore.getState().nodes.map((n) => n.id);
    expect(ids).toEqual(["b"]);
  });

  it("onEdgesChange applies a remove change", () => {
    useTopologyStore.getState().loadCanvas({
      nodes: [makeNode("a"), makeNode("b")],
      edges: [makeEdge("e1", "a", "b")],
      selectedEdgeLayer: "L2",
    });
    useTopologyStore.getState().onEdgesChange([{ id: "e1", type: "remove" }]);
    expect(useTopologyStore.getState().edges).toEqual([]);
  });

  it("addEnrichedEdge adds an edge carrying the supplied layer data", () => {
    useTopologyStore.getState().addDeviceNode(makeNode("a"));
    useTopologyStore.getState().addDeviceNode(makeNode("b"));
    const data: LayerEdgeData = { layer: "L1", pathValid: true, pathHopCount: 3 };
    useTopologyStore
      .getState()
      .addEnrichedEdge({ source: "a", target: "b", sourceHandle: null, targetHandle: null }, data);
    const edges = useTopologyStore.getState().edges;
    expect(edges).toHaveLength(1);
    expect(edges[0].type).toBe("layerEdge");
    expect(edges[0].data).toMatchObject({ layer: "L1", pathValid: true, pathHopCount: 3 });
  });

  it("addEnrichedEdge never dedupes: two same-pair, same-handle edges (e.g. an AI proposal with two role-pair edges) both land (review round 3 item 9)", () => {
    useTopologyStore.getState().addDeviceNode(makeNode("a"));
    useTopologyStore.getState().addDeviceNode(makeNode("b"));
    const connection = { source: "a", target: "b", sourceHandle: null, targetHandle: null };
    useTopologyStore.getState().addEnrichedEdge(connection, { layer: "L1", isProposal: true });
    // addEdge's connectionExists guard (the old implementation) would refuse
    // this second call outright, since it is identical source/target/handles.
    useTopologyStore.getState().addEnrichedEdge(connection, { layer: "L2", isProposal: true });

    const edges = useTopologyStore.getState().edges;
    expect(edges).toHaveLength(2);
    const ids = edges.map((e) => e.id);
    expect(new Set(ids).size).toBe(2);
    expect(edges.map((e) => e.data?.layer).sort()).toEqual(["L1", "L2"]);
  });

  it("acceptProposalNodes clears isProposal on proposal nodes and edges", () => {
    const proposalNode = {
      ...makeNode("p"),
      data: { ...makeNode("p").data, isProposal: true },
    } as Node<DeviceNodeData>;
    const proposalEdge = {
      ...makeEdge("pe", "p", "p"),
      data: { layer: "L2", isProposal: true },
    } as Edge<LayerEdgeData>;
    useTopologyStore.getState().loadCanvas({
      nodes: [makeNode("keep"), proposalNode],
      edges: [makeEdge("ke", "keep", "keep"), proposalEdge],
      selectedEdgeLayer: "L2",
    });

    useTopologyStore.getState().acceptProposalNodes();

    const state = useTopologyStore.getState();
    // All nodes/edges remain, but none are still flagged as a proposal.
    expect(state.nodes).toHaveLength(2);
    expect(state.edges).toHaveLength(2);
    expect(state.nodes.every((n) => !n.data?.isProposal)).toBe(true);
    expect(state.edges.every((e) => !e.data?.isProposal)).toBe(true);
  });

  it("updateEdgePathStatuses commits every update in a single set() call (review item 10a)", () => {
    useTopologyStore.getState().loadCanvas({
      nodes: [makeNode("a"), makeNode("b"), makeNode("c")],
      edges: [makeEdge("e1", "a", "b"), makeEdge("e2", "b", "c")],
      selectedEdgeLayer: "L2",
    });
    const updates = new Map<string, { pathValid: boolean | null; hopCount?: number }>([
      ["e1", { pathValid: true, hopCount: 2 }],
      ["e2", { pathValid: false }],
    ]);
    useTopologyStore.getState().updateEdgePathStatuses(updates);
    const edges = useTopologyStore.getState().edges;
    const e1 = edges.find((e) => e.id === "e1");
    const e2 = edges.find((e) => e.id === "e2");
    expect(e1?.data?.pathValid).toBe(true);
    expect(e1?.data?.pathHopCount).toBe(2);
    expect(e2?.data?.pathValid).toBe(false);
    expect(e2?.data?.pathHopCount).toBeUndefined();
  });

  it("updateEdgePathStatuses is a no-op for an empty update map", () => {
    useTopologyStore.getState().loadCanvas({
      nodes: [makeNode("a"), makeNode("b")],
      edges: [makeEdge("e1", "a", "b")],
      selectedEdgeLayer: "L2",
    });
    const before = useTopologyStore.getState().edges;
    useTopologyStore.getState().updateEdgePathStatuses(new Map());
    expect(useTopologyStore.getState().edges).toBe(before);
  });

  it("updateEdgePathStatuses leaves an edge with no matching update completely untouched", () => {
    useTopologyStore.getState().loadCanvas({
      nodes: [makeNode("a"), makeNode("b"), makeNode("c")],
      edges: [makeEdge("e1", "a", "b"), makeEdge("e2", "b", "c")],
      selectedEdgeLayer: "L2",
    });
    const edgesBefore = useTopologyStore.getState().edges;
    const e2Before = edgesBefore.find((e) => e.id === "e2");
    // Only e1 is updated; e2 has no entry in the map at all (the `!update`
    // passthrough branch), not merely an update that happens to match its
    // current values.
    const updates = new Map<string, { pathValid: boolean | null; hopCount?: number }>([
      ["e1", { pathValid: true, hopCount: 5 }],
    ]);

    useTopologyStore.getState().updateEdgePathStatuses(updates);

    const edgesAfter = useTopologyStore.getState().edges;
    const e1After = edgesAfter.find((e) => e.id === "e1");
    const e2After = edgesAfter.find((e) => e.id === "e2");
    expect(e1After?.data?.pathValid).toBe(true);
    expect(e1After?.data?.pathHopCount).toBe(5);
    // The untouched edge is the SAME object reference, not just equal data:
    // the map() passthrough returns `e` itself for a non-matching id.
    expect(e2After).toBe(e2Before);
    expect(e2After?.data).toBe(e2Before?.data);
  });

  it("removeEdge removes exactly the one edge (review item 2)", () => {
    useTopologyStore.getState().loadCanvas({
      nodes: [makeNode("a"), makeNode("b"), makeNode("c")],
      edges: [makeEdge("e1", "a", "b"), makeEdge("e2", "b", "c")],
      selectedEdgeLayer: "L2",
    });
    useTopologyStore.getState().removeEdge("e1");
    expect(useTopologyStore.getState().edges.map((e) => e.id)).toEqual(["e2"]);
  });

  it("removeEdgesIncidentToNodes removes every edge touching any given node id", () => {
    useTopologyStore.getState().loadCanvas({
      nodes: [makeNode("a"), makeNode("b"), makeNode("c")],
      edges: [makeEdge("e1", "a", "b"), makeEdge("e2", "b", "c"), makeEdge("e3", "a", "c")],
      selectedEdgeLayer: "L2",
    });
    useTopologyStore.getState().removeEdgesIncidentToNodes(["a"]);
    expect(useTopologyStore.getState().edges.map((e) => e.id)).toEqual(["e2"]);
  });

  it("removeEdgesIncidentToNodes is a no-op for an empty node list", () => {
    useTopologyStore.getState().loadCanvas({
      nodes: [makeNode("a"), makeNode("b")],
      edges: [makeEdge("e1", "a", "b")],
      selectedEdgeLayer: "L2",
    });
    const before = useTopologyStore.getState();
    useTopologyStore.getState().removeEdgesIncidentToNodes([]);
    expect(useTopologyStore.getState()).toBe(before);
  });

  it("rejectProposalNodes drops proposal nodes and edges, keeping the rest", () => {
    const proposalNode = {
      ...makeNode("p"),
      data: { ...makeNode("p").data, isProposal: true },
    } as Node<DeviceNodeData>;
    const proposalEdge = {
      ...makeEdge("pe", "p", "p"),
      data: { layer: "L2", isProposal: true },
    } as Edge<LayerEdgeData>;
    useTopologyStore.getState().loadCanvas({
      nodes: [makeNode("keep"), proposalNode],
      edges: [makeEdge("ke", "keep", "keep"), proposalEdge],
      selectedEdgeLayer: "L2",
    });

    useTopologyStore.getState().rejectProposalNodes();

    const state = useTopologyStore.getState();
    expect(state.nodes.map((n) => n.id)).toEqual(["keep"]);
    expect(state.edges.map((e) => e.id)).toEqual(["ke"]);
  });
});
