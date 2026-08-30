import { http, HttpResponse } from "msw";
import { render, screen, waitFor, fireEvent, act } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import type { Node } from "@xyflow/react";

// Covers TopologyEditorPage.tsx surfaces the other TopologyEditor*.test.tsx
// files leave untouched: handleElementAttachConfirm/Cancel (Network Elements
// mocks ElementAttachDialog but never fires its onConfirm/onCancel), a plain
// device drag-drop (only the dynamic-template and network-element drop
// branches are covered elsewhere), the existingWiredElementDevicePortIds
// derivation with a pre-existing canvas edge, and handleAIProposal's edge
// mapping (role to node id, an edge whose role does not resolve).

const { toastError, toastSuccess } = vi.hoisted(() => ({
  toastError: vi.fn(),
  toastSuccess: vi.fn(),
}));
vi.mock("react-hot-toast", () => ({
  default: Object.assign(
    (msg: string) => toastSuccess(msg),
    { error: toastError, success: toastSuccess, custom: vi.fn(), dismiss: vi.fn() },
  ),
}));

const rfProps = vi.hoisted(() => ({ current: null as Record<string, unknown> | null }));
vi.mock("@xyflow/react", async () => {
  const actual = await vi.importActual<typeof import("@xyflow/react")>("@xyflow/react");
  return {
    ...actual,
    ReactFlow: (props: Record<string, unknown> & { children?: React.ReactNode }) => {
      rfProps.current = props;
      return (
        <div
          data-testid="react-flow"
          onDrop={props.onDrop as React.DragEventHandler}
          onDragOver={props.onDragOver as React.DragEventHandler}
        >
          {props.children as React.ReactNode}
        </div>
      );
    },
    Background: () => <div data-testid="rf-background" />,
    Controls: () => <div data-testid="rf-controls" />,
    MiniMap: () => <div data-testid="rf-minimap" />,
  };
});

vi.mock("@/api/inventory", async () => {
  const actual = await vi.importActual<typeof import("@/api/inventory")>("@/api/inventory");
  return { ...actual, hydrateCanvasNodes: (d: unknown) => Promise.resolve(d) };
});

vi.mock("@/components/equipment-browser/EquipmentBrowser", () => ({
  EquipmentBrowser: () => <div data-testid="equipment-browser" />,
}));

// Stub the attach dialog to drive the page's real onConfirm/onCancel handlers
// directly, mirroring TopologyEditorWiring.test.tsx's stub of the same
// component. This file is about the PAGE's own handlers, not the dialog's
// internal port-selection UI (covered by ElementAttachDialog's own tests).
const elementAttachProps = vi.hoisted(() => ({ current: null as Record<string, unknown> | null }));
vi.mock("@/components/topology-editor/ElementAttachDialog", () => ({
  ElementAttachDialog: (props: Record<string, unknown>) => {
    elementAttachProps.current = props;
    return <div data-testid="element-attach-dialog" />;
  },
}));

// Stub AIDialog to drive handleAIProposal's edge-mapping branch directly.
const aiDialogProps = vi.hoisted(() => ({ current: null as Record<string, unknown> | null }));
vi.mock("@/components/topology-editor/AIDialog", () => ({
  AIDialog: (props: Record<string, unknown>) => {
    aiDialogProps.current = props;
    return props.open ? <div data-testid="ai-dialog" /> : null;
  },
}));

import { server } from "../mocks/server";
import { TopologyEditorPage } from "@/pages/TopologyEditorPage";
import { useTopologyStore } from "@/stores/topologyStore";
import type { CanvasNodeData } from "@/types/topology.types";
import type { AIGenerateResponse } from "@/types/ai.types";

const TOPO_ID = "topo-attach-1";

const PARENT_TOPOLOGY = {
  id: TOPO_ID,
  name: "Parent topology",
  created_by: "u",
  owner_name: "u",
  created_at: "2026-05-01T00:00:00Z",
  updated_at: "2026-05-01T00:00:00Z",
  canvas_data: null,
};

function deviceNode(id: string, deviceId: string): Node<CanvasNodeData> {
  return {
    id,
    type: "deviceNode",
    position: { x: 0, y: 0 },
    data: {
      device: { id: deviceId, name: deviceId, topology_type: "PHYSICAL", status: "AVAILABLE" },
      label: deviceId,
      topologyType: "PHYSICAL",
    } as CanvasNodeData,
  };
}

function elementNode(id: string, label = "VLAN A"): Node<CanvasNodeData> {
  return {
    id,
    type: "networkElementNode",
    position: { x: 100, y: 100 },
    data: { element: { id: `elem-${id}`, element_type: "vlan_segment", label, attrs: {} } } as CanvasNodeData,
  };
}

function baseHandlers() {
  return [
    http.get(`/api/cabling/topologies/${TOPO_ID}`, () => HttpResponse.json(PARENT_TOPOLOGY)),
    http.get("/api/reservations/", () =>
      HttpResponse.json({ items: [], total: 0, skip: 0, limit: 500 }),
    ),
    http.get("/api/ai/status", () => HttpResponse.json({ enabled: true })),
  ];
}

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[`/topology/${TOPO_ID}`]}>
        <Routes>
          <Route path="/topology/:id" element={<TopologyEditorPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

async function renderPageWithCanvas(nodes: Node<CanvasNodeData>[]) {
  const view = renderPage();
  await screen.findByText("Parent topology");
  act(() => {
    useTopologyStore.setState({ nodes, edges: [], selectedEdgeLayer: "L2" });
  });
  return view;
}

function dropDevice(payload: string) {
  fireEvent.drop(screen.getByTestId("react-flow"), {
    dataTransfer: {
      getData: (type: string) => (type === "application/herd-device" ? payload : ""),
    },
  });
}

beforeAll(() => {
  HTMLDialogElement.prototype.showModal = vi.fn(function (this: HTMLDialogElement) {
    this.open = true;
  });
  HTMLDialogElement.prototype.close = vi.fn(function (this: HTMLDialogElement) {
    this.open = false;
  });
});

beforeEach(() => {
  useTopologyStore.setState({ nodes: [], edges: [], selectedEdgeLayer: "L2" });
});

afterEach(() => {
  vi.clearAllMocks();
  rfProps.current = null;
  elementAttachProps.current = null;
  aiDialogProps.current = null;
  useTopologyStore.setState({ nodes: [], edges: [], selectedEdgeLayer: "L2" });
});

describe("TopologyEditorPage plain device drag-drop", () => {
  it("dropping a device creates a deviceNode at the drop position", async () => {
    server.use(...baseHandlers());
    renderPage();
    await screen.findByTestId("react-flow");

    const payload = JSON.stringify({
      id: "d-new",
      name: "new-switch",
      topology_type: "PHYSICAL",
      status: "AVAILABLE",
    });
    dropDevice(payload);

    await waitFor(() => {
      const node = useTopologyStore.getState().nodes.find((n) => n.type === "deviceNode");
      expect(node).toBeDefined();
    });
    const node = useTopologyStore.getState().nodes.find((n) => n.type === "deviceNode")!;
    expect((node.data as { label: string }).label).toBe("new-switch");
  });

  it("dropping a device already on the canvas is a no-op (no duplicate node)", async () => {
    server.use(...baseHandlers());
    await renderPageWithCanvas([deviceNode("n-dev", "d-existing")]);

    const payload = JSON.stringify({
      id: "d-existing",
      name: "existing",
      topology_type: "PHYSICAL",
      status: "AVAILABLE",
    });
    dropDevice(payload);

    // Give any (incorrect) async add a tick to land, then assert nothing changed.
    await new Promise((r) => setTimeout(r, 0));
    expect(useTopologyStore.getState().nodes).toHaveLength(1);
  });

  it("a drop with no device/template/element payload is a no-op", async () => {
    server.use(...baseHandlers());
    renderPage();
    await screen.findByTestId("react-flow");

    fireEvent.drop(screen.getByTestId("react-flow"), {
      dataTransfer: { getData: () => "" },
    });

    await new Promise((r) => setTimeout(r, 0));
    expect(useTopologyStore.getState().nodes).toHaveLength(0);
  });
});

describe("TopologyEditorPage handleElementAttachConfirm/Cancel", () => {
  it("confirm adds one attachment edge per selected port, device as source, and clears the pending state", async () => {
    server.use(...baseHandlers());
    await renderPageWithCanvas([deviceNode("n-dev", "d-1"), elementNode("n-elem", "Mgmt VLAN")]);

    const connection = { source: "n-dev", target: "n-elem", sourceHandle: null, targetHandle: null };
    act(() => {
      (rfProps.current?.onConnect as (c: typeof connection) => void)(connection);
    });
    await screen.findByTestId("element-attach-dialog");

    const onConfirm = elementAttachProps.current?.onConfirm as (
      selections: Array<{ portId: string; portName: string }>,
    ) => void;
    act(() =>
      onConfirm([
        { portId: "p1", portName: "eth1" },
        { portId: "p2", portName: "eth2" },
      ]),
    );

    await waitFor(() => expect(useTopologyStore.getState().edges).toHaveLength(2));
    const edges = useTopologyStore.getState().edges;
    expect(edges.every((e) => e.source === "n-dev" && e.target === "n-elem")).toBe(true);
    expect(edges.map((e) => e.data?.source_port_name)).toEqual(["eth1", "eth2"]);
    // The pending attach state cleared: the dialog is gone.
    expect(screen.queryByTestId("element-attach-dialog")).not.toBeInTheDocument();
  });

  it("cancel clears the pending attach state and adds no edge", async () => {
    server.use(...baseHandlers());
    await renderPageWithCanvas([deviceNode("n-dev", "d-1"), elementNode("n-elem")]);

    const connection = { source: "n-dev", target: "n-elem", sourceHandle: null, targetHandle: null };
    act(() => {
      (rfProps.current?.onConnect as (c: typeof connection) => void)(connection);
    });
    await screen.findByTestId("element-attach-dialog");

    const onCancel = elementAttachProps.current?.onCancel as () => void;
    act(() => onCancel());

    await waitFor(() =>
      expect(screen.queryByTestId("element-attach-dialog")).not.toBeInTheDocument(),
    );
    expect(useTopologyStore.getState().edges).toHaveLength(0);
  });

  it("existingWiredElementDevicePortIds includes a port already wired to the pending device by an existing edge", async () => {
    server.use(...baseHandlers());
    await renderPageWithCanvas([deviceNode("n-dev", "d-1"), elementNode("n-elem")]);
    act(() => {
      useTopologyStore.setState({
        edges: [
          {
            id: "existing-e1",
            source: "n-dev",
            target: "n-elem",
            type: "layerEdge",
            data: { layer: "L2", source_port_id: "p-already-wired", source_port_name: "eth0" },
          },
        ],
      });
    });

    const connection = { source: "n-dev", target: "n-elem", sourceHandle: null, targetHandle: null };
    act(() => {
      (rfProps.current?.onConnect as (c: typeof connection) => void)(connection);
    });
    await screen.findByTestId("element-attach-dialog");

    const wired = elementAttachProps.current?.existingWiredPortIds as ReadonlySet<string>;
    expect(wired.has("p-already-wired")).toBe(true);
  });
});

describe("TopologyEditorPage handleAIProposal edge mapping", () => {
  it("maps proposal edges by role to the newly created node ids", async () => {
    server.use(...baseHandlers());
    await renderPageWithCanvas([]);

    const response: AIGenerateResponse = {
      purpose: "Two-node proposal",
      devices: [
        { role: "leaf1", device: { id: "d-1", name: "leaf1", topology_type: "PHYSICAL", status: "AVAILABLE" } },
        { role: "leaf2", device: { id: "d-2", name: "leaf2", topology_type: "PHYSICAL", status: "AVAILABLE" } },
      ],
      edges: [{ source_role: "leaf1", target_role: "leaf2", layer: "L2" }],
      notes: "",
      file_summaries: [],
    } as unknown as AIGenerateResponse;

    const onProposal = aiDialogProps.current?.onProposal as (r: AIGenerateResponse) => void;
    act(() => onProposal(response));

    await waitFor(() => expect(useTopologyStore.getState().edges).toHaveLength(1));
    const edge = useTopologyStore.getState().edges[0];
    const nodes = useTopologyStore.getState().nodes;
    const leaf1Node = nodes.find((n) => (n.data as { device?: { id?: string } }).device?.id === "d-1")!;
    const leaf2Node = nodes.find((n) => (n.data as { device?: { id?: string } }).device?.id === "d-2")!;
    expect(edge.source).toBe(leaf1Node.id);
    expect(edge.target).toBe(leaf2Node.id);
    expect(edge.data?.layer).toBe("L2");
    expect(edge.data?.isProposal).toBe(true);
  });

  it("skips an edge whose role does not resolve to a proposed device, without crashing", async () => {
    server.use(...baseHandlers());
    await renderPageWithCanvas([]);

    const response: AIGenerateResponse = {
      purpose: "Dangling role edge",
      devices: [
        { role: "leaf1", device: { id: "d-1", name: "leaf1", topology_type: "PHYSICAL", status: "AVAILABLE" } },
      ],
      edges: [{ source_role: "leaf1", target_role: "does-not-exist", layer: "L2" }],
      notes: "",
      file_summaries: [],
    } as unknown as AIGenerateResponse;

    const onProposal = aiDialogProps.current?.onProposal as (r: AIGenerateResponse) => void;
    expect(() => act(() => onProposal(response))).not.toThrow();

    await waitFor(() =>
      expect(useTopologyStore.getState().nodes.some((n) => n.type === "deviceNode")).toBe(true),
    );
    // The one proposed device landed; no edge was added for the dangling role.
    expect(useTopologyStore.getState().edges).toHaveLength(0);
  });
});
