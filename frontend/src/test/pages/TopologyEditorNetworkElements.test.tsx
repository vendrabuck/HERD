import { http, HttpResponse } from "msw";
import { render, screen, waitFor, fireEvent, act } from "@testing-library/react";
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

// React Flow renders a heavy canvas that does not work in jsdom. Stub the
// visual component but capture its props so tests can drive the page's real
// onDrop/isValidConnection/onConnect handlers, and forward onDrop so drop
// events reach the page. Mirrors TopologyEditorDynamicPlaceholders.test.tsx.
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

// Stub the attach dialog: TopologyEditorPage wiring (which dialog opens, with
// which props) is what this file tests; ElementAttachDialog's own internals
// (port selection, confirm building N edges) are covered by its own test file.
const elementAttachProps = vi.hoisted(() => ({ current: null as Record<string, unknown> | null }));
vi.mock("@/components/topology-editor/ElementAttachDialog", () => ({
  ElementAttachDialog: (props: Record<string, unknown>) => {
    elementAttachProps.current = props;
    return <div data-testid="element-attach-dialog" />;
  },
}));

import { server } from "../mocks/server";
import { TopologyEditorPage } from "@/pages/TopologyEditorPage";
import { useTopologyStore } from "@/stores/topologyStore";
import type { CanvasNodeData, NetworkElementNodeData } from "@/types/topology.types";

const TOPO_ID = "topo-1";

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

function elementNode(id: string, label = "VLAN segment"): Node<CanvasNodeData> {
  return {
    id,
    type: "networkElementNode",
    position: { x: 100, y: 100 },
    data: {
      element: { id: `elem-${id}`, element_type: "vlan_segment", label, attrs: {} },
    } as CanvasNodeData,
  };
}

function placeholderNode(id: string, templateId: string, count: number): Node<CanvasNodeData> {
  return {
    id,
    type: "dynamicPlaceholderNode",
    position: { x: 100, y: 100 },
    data: { templateId, templateName: "Ubuntu VM", templateIcon: null, count } as CanvasNodeData,
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

async function renderPageWithCanvas(nodes: Node<CanvasNodeData>[]) {
  const view = renderPage();
  await screen.findByText("Parent topology");
  act(() => {
    useTopologyStore.setState({ nodes, edges: [], selectedEdgeLayer: "L2" });
  });
  return view;
}

function dropNetworkElement(payload: string) {
  fireEvent.drop(screen.getByTestId("react-flow"), {
    dataTransfer: {
      getData: (type: string) => (type === "application/herd-network-element" ? payload : ""),
    },
  });
}

function elementNodes() {
  return useTopologyStore.getState().nodes.filter((n) => n.type === "networkElementNode");
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
  useTopologyStore.setState({ nodes: [], edges: [], selectedEdgeLayer: "L2" });
});

describe("TopologyEditorPage network elements (ADR 0012 phase 2)", () => {
  it("dropping a network element creates a networkElementNode with a fresh client-minted UUID element id", async () => {
    server.use(...baseHandlers());
    renderPage();
    await screen.findByTestId("react-flow");

    const payload = JSON.stringify({ element_type: "vlan_segment", label: "VLAN segment" });
    dropNetworkElement(payload);

    await waitFor(() => expect(elementNodes()).toHaveLength(1));
    const data = elementNodes()[0].data as NetworkElementNodeData;
    expect(data.element.element_type).toBe("vlan_segment");
    expect(data.element.label).toBe("VLAN segment");
    // A UUID (crypto.randomUUID shape), not a template-derived or reused id.
    expect(data.element.id).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i);
  });

  it("allows multiple elements of the same type, unlike the one-placeholder-per-template rule", async () => {
    server.use(...baseHandlers());
    renderPage();
    await screen.findByTestId("react-flow");

    const payload = JSON.stringify({ element_type: "vlan_segment", label: "VLAN segment" });
    dropNetworkElement(payload);
    await waitFor(() => expect(elementNodes()).toHaveLength(1));
    dropNetworkElement(payload);
    await waitFor(() => expect(elementNodes()).toHaveLength(2));

    // Each drop mints its own distinct element id.
    const ids = elementNodes().map((n) => (n.data as NetworkElementNodeData).element.id);
    expect(new Set(ids).size).toBe(2);
  });

  it("isValidConnection accepts a device-to-element connection", async () => {
    server.use(...baseHandlers());
    await renderPageWithCanvas([deviceNode("n-dev", "d-1"), elementNode("n-elem")]);

    const isValidConnection = rfProps.current?.isValidConnection as (c: {
      source: string;
      target: string;
    }) => boolean;
    expect(isValidConnection({ source: "n-dev", target: "n-elem" })).toBe(true);
    expect(isValidConnection({ source: "n-elem", target: "n-dev" })).toBe(true);
    expect(toastError).not.toHaveBeenCalled();
  });

  it("isValidConnection refuses element-to-element with the exact toast text and creates no edge", async () => {
    server.use(...baseHandlers());
    await renderPageWithCanvas([elementNode("n-elem-1", "VLAN A"), elementNode("n-elem-2", "VLAN B")]);

    const fullConnection = {
      source: "n-elem-1",
      target: "n-elem-2",
      sourceHandle: null,
      targetHandle: null,
    };
    const isValidConnection = rfProps.current?.isValidConnection as (
      c: typeof fullConnection,
    ) => boolean;
    expect(isValidConnection(fullConnection)).toBe(false);
    expect(toastError).toHaveBeenCalledWith("Network elements cannot be linked to each other", {
      id: "element-to-element",
    });

    // Even if React Flow invoked onConnect anyway, handleConnect's own guard
    // drops it: no dialog opens and no edge lands in the store.
    (rfProps.current?.onConnect as (c: typeof fullConnection) => void)(fullConnection);
    expect(useTopologyStore.getState().edges).toHaveLength(0);
    expect(screen.queryByTestId("element-attach-dialog")).not.toBeInTheDocument();
  });

  it("connecting a device to an element opens ElementAttachDialog with the right device and element props", async () => {
    server.use(...baseHandlers());
    await renderPageWithCanvas([deviceNode("n-dev", "d-1"), elementNode("n-elem", "Mgmt VLAN")]);

    const connection = { source: "n-dev", target: "n-elem", sourceHandle: null, targetHandle: null };
    act(() => {
      (rfProps.current?.onConnect as (c: typeof connection) => void)(connection);
    });

    expect(await screen.findByTestId("element-attach-dialog")).toBeInTheDocument();
    expect(elementAttachProps.current).toMatchObject({
      open: true,
      deviceId: "d-1",
      deviceName: "d-1",
      elementLabel: "Mgmt VLAN",
      elementType: "vlan_segment",
    });
  });

  it("persistableCanvas KEEPS network element nodes and their edges (the placeholder-opposite rule)", async () => {
    let putBody: Record<string, unknown> | null = null;
    server.use(
      ...baseHandlers(),
      http.put(`/api/cabling/topologies/${TOPO_ID}`, async ({ request }) => {
        putBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(PARENT_TOPOLOGY);
      }),
    );
    await renderPageWithCanvas([
      deviceNode("n-dev", "d-1"),
      elementNode("n-elem", "Mgmt VLAN"),
      placeholderNode("n-ph", "dt-1", 2),
    ]);
    act(() => {
      useTopologyStore.setState((s) => ({
        edges: [
          {
            id: "e-attach",
            source: "n-dev",
            target: "n-elem",
            type: "layerEdge",
            data: { layer: "L2", source_port_name: "eth0" },
          },
          ...s.edges,
        ],
      }));
    });

    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(toastSuccess).toHaveBeenCalled());
    const canvas = (putBody as Record<string, unknown> | null)?.canvas_data as {
      nodes: Array<{ id: string }>;
      edges: Array<{ id: string }>;
    };
    // The element node and its attachment edge persist...
    expect(canvas.nodes.map((n) => n.id)).toContain("n-elem");
    expect(canvas.edges.map((e) => e.id)).toContain("e-attach");
    // ...while the placeholder is still stripped, exactly as before ADR 0012.
    expect(canvas.nodes.map((n) => n.id)).not.toContain("n-ph");
  });

  it("allDeviceIds excludes network element nodes (no data.device to read)", async () => {
    server.use(...baseHandlers());
    await renderPageWithCanvas([deviceNode("n-dev", "d-1"), elementNode("n-elem")]);

    // Reserve Topology's device count reads allDeviceIds; an element must not
    // inflate it or throw reading a nonexistent .device.id.
    expect(screen.getByRole("button", { name: /Reserve Topology \(1 device\)/ })).toBeInTheDocument();
  });

});
