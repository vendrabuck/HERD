import { http, HttpResponse } from "msw";
import { render, screen, waitFor, act } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import type { Node } from "@xyflow/react";

const { toastError, toastSuccess, toastCustom, toastDismiss } = vi.hoisted(() => ({
  toastError: vi.fn(),
  toastSuccess: vi.fn(),
  toastCustom: vi.fn(),
  toastDismiss: vi.fn(),
}));
vi.mock("react-hot-toast", () => ({
  default: Object.assign(
    (msg: string) => toastSuccess(msg),
    { error: toastError, success: toastSuccess, custom: toastCustom, dismiss: toastDismiss },
  ),
}));

// Issue #989: the seeded "BROKEN - Half-Wired Chain" topology stores a node
// typed deviceNode whose data has no `device` between two device nodes, and
// opening it in the editor landed on the ErrorBoundary ("Cannot use 'in'
// operator to search for 'id' in undefined", from persistableDevice). React
// Flow is stubbed as in TopologyEditorNetworkElements.test.tsx so the page's
// real handlers can be driven; DeviceNode's own degraded render is pinned in
// test/components/DeviceNode.test.tsx.
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
          {props.children}
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


import { server } from "../mocks/server";
import { TopologyEditorPage } from "@/pages/TopologyEditorPage";
import { useTopologyStore } from "@/stores/topologyStore";
import type { CanvasNodeData } from "@/types/topology.types";

const TOPO_ID = "topo-1";

const PARENT_TOPOLOGY = {
  id: TOPO_ID,
  name: "Parent topology",
  created_by: "u",
  owner_name: "u",
  created_at: "2026-05-01T00:00:00Z",
  updated_at: "2026-05-01T00:00:00Z",
  canvas_data: null as unknown,
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

function baseHandlers() {
  return [
    http.get(`/api/cabling/topologies/${TOPO_ID}`, () => HttpResponse.json(PARENT_TOPOLOGY)),
    http.get("/api/reservations/", () =>
      HttpResponse.json({ items: [], total: 0, skip: 0, limit: 500 }),
    ),
    http.get("/api/ai/status", () => HttpResponse.json({ enabled: false })),
    http.get("/api/inventory/templates", () =>
      HttpResponse.json({ items: [], total: 0, skip: 0, limit: 500 }),
    ),
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


// The node shape seedtools/topologies.py build_canvas emits for a None slot.
function deviceLessNode(id: string): Node<CanvasNodeData> {
  return {
    id,
    type: "deviceNode",
    position: { x: 200, y: 0 },
    data: {} as CanvasNodeData,
  };
}

const HALF_WIRED_CHAIN = {
  nodes: [deviceNode("n0", "d-1"), deviceLessNode("n1"), deviceNode("n2", "d-2")],
  edges: [
    { id: "e0", source: "n0", target: "n1", data: { layer: "L2", isProposal: false } },
    { id: "e1", source: "n1", target: "n2", data: { layer: "L2", isProposal: false } },
  ],
};

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
  // The override comes first: within one server.use call the earlier handler wins.
  server.use(
    http.get(`/api/cabling/topologies/${TOPO_ID}`, () =>
      HttpResponse.json({ ...PARENT_TOPOLOGY, canvas_data: HALF_WIRED_CHAIN }),
    ),
    ...baseHandlers(),
  );
});

afterEach(() => {
  vi.clearAllMocks();
  rfProps.current = null;
  useTopologyStore.setState({ nodes: [], edges: [], selectedEdgeLayer: "L2" });
});

async function openHalfWiredChain() {
  renderPage();
  await screen.findByText("Parent topology");
  await waitFor(() => expect(useTopologyStore.getState().nodes).toHaveLength(3));
  await screen.findByTestId("react-flow");
}

describe("TopologyEditorPage with a device-less node (issue #989)", () => {
  it("opens the seeded half-wired chain without reaching the error boundary", async () => {
    await openHalfWiredChain();
    expect(screen.queryByText(/something went wrong/i)).not.toBeInTheDocument();
    expect(useTopologyStore.getState().edges).toHaveLength(2);
  });

  it("refuses a new connection to the device-less node with a toast instead of throwing", async () => {
    await openHalfWiredChain();
    const isValidConnection = rfProps.current?.isValidConnection as (c: {
      source: string;
      target: string;
    }) => boolean;
    expect(isValidConnection({ source: "n0", target: "n1" })).toBe(false);
    expect(isValidConnection({ source: "n1", target: "n2" })).toBe(false);
    expect(toastError).toHaveBeenCalledWith(
      "This node has no device: remove it or replace it with a device from the browser",
      { id: "device-less-node" },
    );
    // A connection between the two real devices is still judged normally.
    expect(isValidConnection({ source: "n0", target: "n2" })).toBe(true);
  });

  it("onConnect to the device-less node opens no dialog and does not throw", async () => {
    await openHalfWiredChain();
    const onConnect = rfProps.current?.onConnect as (c: Record<string, unknown>) => void;
    expect(() =>
      act(() => {
        onConnect({ source: "n1", target: "n2", sourceHandle: null, targetHandle: null });
      }),
    ).not.toThrow();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("selecting, moving, and deleting the node and its edges does not throw", async () => {
    await openHalfWiredChain();
    const onNodesChange = rfProps.current?.onNodesChange as (c: unknown[]) => void;
    const onEdgesChange = rfProps.current?.onEdgesChange as (c: unknown[]) => void;
    act(() => {
      onNodesChange([{ type: "select", id: "n1", selected: true }]);
    });
    act(() => {
      onNodesChange([{ type: "position", id: "n1", position: { x: 300, y: 50 } }]);
    });
    act(() => {
      onEdgesChange([
        { type: "remove", id: "e0" },
        { type: "remove", id: "e1" },
      ]);
    });
    act(() => {
      onNodesChange([{ type: "remove", id: "n1" }]);
    });
    await waitFor(() => expect(useTopologyStore.getState().nodes).toHaveLength(2));
    expect(useTopologyStore.getState().edges).toHaveLength(0);
    expect(screen.getByTestId("react-flow")).toBeInTheDocument();
  });
});
