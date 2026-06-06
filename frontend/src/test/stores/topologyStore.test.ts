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
});
