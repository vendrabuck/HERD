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
// events reach the page.
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

// Skip the inventory device re-fetch: hydrate is identity here.
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
import type {
  CanvasNodeData,
  DynamicPlaceholderNodeData,
} from "@/types/topology.types";

const TOPO_ID = "topo-1";

const DYNAMIC_TEMPLATE = {
  id: "dt-1",
  name: "Ubuntu VM",
  template_type: "dynamic",
  icon: null,
};

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

function placeholderNode(id: string, templateId: string, count: number): Node<CanvasNodeData> {
  return {
    id,
    type: "dynamicPlaceholderNode",
    position: { x: 100, y: 100 },
    data: {
      templateId,
      templateName: "Ubuntu VM",
      templateIcon: null,
      count,
    } satisfies DynamicPlaceholderNodeData,
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
      HttpResponse.json({ items: [DYNAMIC_TEMPLATE], total: 1, skip: 0, limit: 500 }),
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

// The page's load effect clears the canvas once the (canvas-less) topology
// resolves, so seeding must happen after that: render, wait for the topology
// name to land in the toolbar, then set the store.
async function renderPageWithCanvas(nodes: Node<CanvasNodeData>[]) {
  const view = renderPage();
  await screen.findByText("Parent topology");
  act(() => {
    useTopologyStore.setState({ nodes, edges: [], selectedEdgeLayer: "L2" });
  });
  return view;
}

function dropDynamicTemplate(payload: string) {
  fireEvent.drop(screen.getByTestId("react-flow"), {
    dataTransfer: {
      getData: (type: string) =>
        type === "application/herd-dynamic-template" ? payload : "",
    },
  });
}

function placeholders() {
  return useTopologyStore.getState().nodes.filter((n) => n.type === "dynamicPlaceholderNode");
}

function fillTimes() {
  fireEvent.change(screen.getByLabelText("Start time"), {
    target: { value: "2026-06-01T10:00" },
  });
  fireEvent.change(screen.getByLabelText("End time"), {
    target: { value: "2026-06-01T12:00" },
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
  useTopologyStore.setState({ nodes: [], edges: [], selectedEdgeLayer: "L2" });
});

describe("TopologyEditorPage dynamic placeholders", () => {
  it("dropping a dynamic template creates one placeholder with count 1; re-dropping the same template is a no-op", async () => {
    server.use(...baseHandlers());
    renderPage();
    await screen.findByTestId("react-flow");

    const payload = JSON.stringify({ id: "dt-1", name: "Ubuntu VM", icon: null });
    dropDynamicTemplate(payload);

    await waitFor(() => expect(placeholders()).toHaveLength(1));
    const data = placeholders()[0].data as DynamicPlaceholderNodeData;
    expect(data.templateId).toBe("dt-1");
    expect(data.templateName).toBe("Ubuntu VM");
    expect(data.count).toBe(1);

    // One placeholder per template: the count is edited on the node instead.
    dropDynamicTemplate(payload);
    expect(placeholders()).toHaveLength(1);
  });

  it("refuses a connection to a placeholder with a toast and creates no edge", async () => {
    server.use(...baseHandlers());
    await renderPageWithCanvas([deviceNode("n-dev", "d-1"), placeholderNode("n-ph", "dt-1", 1)]);

    const connection = {
      source: "n-dev",
      target: "n-ph",
      sourceHandle: null,
      targetHandle: null,
    };
    const isValidConnection = rfProps.current?.isValidConnection as (
      c: typeof connection,
    ) => boolean;
    expect(isValidConnection(connection)).toBe(false);
    expect(toastError).toHaveBeenCalledWith(
      "Dynamic placeholders have no ports until the reservation activates",
      { id: "dynamic-placeholder" },
    );

    // Even if React Flow invoked onConnect anyway, the guard drops it: no port
    // modal opens and no edge lands in the store.
    (rfProps.current?.onConnect as (c: typeof connection) => void)(connection);
    expect(useTopologyStore.getState().edges).toHaveLength(0);
  });

  it("reserving sends device_ids plus dynamic_requests expanded count-per-template", async () => {
    let body: Record<string, unknown> | null = null;
    server.use(
      ...baseHandlers(),
      http.post("/api/reservations/", async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({ id: "r-1" });
      }),
    );
    await renderPageWithCanvas([deviceNode("n-dev", "d-1"), placeholderNode("n-ph", "dt-1", 2)]);

    fireEvent.click(screen.getByRole("button", { name: /Reserve Topology/ }));

    // The modal prefills one editable entry per placeholder (template x count).
    expect(await screen.findByLabelText("Instance count 1")).toHaveValue(2);

    fillTimes();
    fireEvent.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith("Reservation created"));
    expect(body).toMatchObject({
      device_ids: ["d-1"],
      topology_id: TOPO_ID,
      dynamic_requests: [{ template_id: "dt-1" }, { template_id: "dt-1" }],
    });
  });

  it("enables Reserve for a placeholder-only canvas (dynamic-only booking)", async () => {
    server.use(...baseHandlers());
    await renderPageWithCanvas([placeholderNode("n-ph", "dt-1", 1)]);

    expect(screen.getByRole("button", { name: /Reserve Topology/ })).toBeEnabled();
  });

  it("omits dynamic_requests entirely when the canvas has no placeholders", async () => {
    let body: Record<string, unknown> | null = null;
    server.use(
      ...baseHandlers(),
      http.post("/api/reservations/", async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({ id: "r-2" });
      }),
    );
    await renderPageWithCanvas([deviceNode("n-dev", "d-1")]);

    fireEvent.click(screen.getByRole("button", { name: /Reserve Topology/ }));
    fillTimes();
    fireEvent.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith("Reservation created"));
    expect(body).toMatchObject({ device_ids: ["d-1"] });
    // The pre-dynamic wire shape: the key is omitted, not sent as [].
    expect(body).not.toHaveProperty("dynamic_requests");
  });

  it("saving the parent topology excludes placeholder nodes from canvas_data", async () => {
    let putBody: Record<string, unknown> | null = null;
    server.use(
      ...baseHandlers(),
      http.put(`/api/cabling/topologies/${TOPO_ID}`, async ({ request }) => {
        putBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(PARENT_TOPOLOGY);
      }),
    );
    await renderPageWithCanvas([deviceNode("n-dev", "d-1"), placeholderNode("n-ph", "dt-1", 3)]);

    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith("Topology saved"));
    const canvas = (putBody as Record<string, unknown> | null)?.canvas_data as {
      nodes: Array<{ id: string }>;
    };
    expect(canvas.nodes.map((n) => n.id)).toEqual(["n-dev"]);
    // The placeholder stays on the live canvas as a planning artifact.
    expect(placeholders()).toHaveLength(1);
  });
});
