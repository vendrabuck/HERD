import { http, HttpResponse } from "msw";
import { render, screen, waitFor, fireEvent, act } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import type { Node } from "@xyflow/react";

// ADR 0014 phase 2 (issue #34): the plain-topology-save half of the Routing
// panel feature (E1 persistence, E5's validate-after-save gating, toast, and
// per-row reason rendering). The fork-save half (l3_intent_invalid/
// l3_intent_malformed/l3_config_unavailable) is covered in
// TopologyEditorForkMode.test.tsx alongside the rest of that file's fork-save
// error branches.

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
      return <div data-testid="react-flow">{props.children as React.ReactNode}</div>;
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
import type { CanvasNodeData, DeviceNodeData, L3RouteIntent } from "@/types/topology.types";

const TOPO_ID = "topo-l3-1";

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

function l3Node(
  id: string,
  deviceId: string,
  routes: L3RouteIntent[],
  selected = false,
): Node<CanvasNodeData> {
  return {
    id,
    type: "deviceNode",
    selected,
    position: { x: 0, y: 0 },
    data: {
      device: {
        id: deviceId,
        name: deviceId,
        topology_type: "PHYSICAL",
        status: "AVAILABLE",
        connection_type: "Layer 3 Switch",
      },
      label: deviceId,
      topologyType: "PHYSICAL",
      l3: { routes },
    } as CanvasNodeData,
  };
}

function route(overrides: Partial<L3RouteIntent> = {}): L3RouteIntent {
  return {
    destination: "10.0.0.0/24",
    next_hop: null,
    interface: "eth0",
    virtual_router: null,
    ...overrides,
  };
}

const PARENT_TOPOLOGY = {
  id: TOPO_ID,
  name: "Parent topology",
  created_by: "u",
  owner_name: "u",
  created_at: "2026-05-01T00:00:00Z",
  updated_at: "2026-05-01T00:00:00Z",
  canvas_data: null,
};

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
    http.get(`/api/cabling/topologies/${TOPO_ID}/versions`, () =>
      HttpResponse.json({ items: [], total: 0, skip: 0, limit: 200 }),
    ),
    // A device's config-version list, hit by the Routing panel's Import
    // button whenever it mounts for a selected L3 switch; not under test
    // here, so an empty page is enough to keep it quiet.
    http.get("/api/inventory/devices/:deviceId/config-versions", () =>
      HttpResponse.json({ items: [], total: 0, skip: 0, limit: 50 }),
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
          <Route path="/topology" element={<div>topology list page</div>} />
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

beforeAll(() => {
  HTMLDialogElement.prototype.showModal = vi.fn(function (this: HTMLDialogElement) {
    this.open = true;
  });
  HTMLDialogElement.prototype.close = vi.fn(function (this: HTMLDialogElement) {
    this.open = false;
  });
});

beforeEach(() => {
  server.use(...baseHandlers());
  useTopologyStore.setState({ nodes: [], edges: [], selectedEdgeLayer: "L2" });
});

afterEach(() => {
  vi.clearAllMocks();
  rfProps.current = null;
  useTopologyStore.setState({ nodes: [], edges: [], selectedEdgeLayer: "L2" });
});

describe("TopologyEditorPage plain save: L3 routing intent (ADR 0014 phase 2, issue #34)", () => {
  it("persists data.l3 through the save PUT unchanged (E1 persistableCanvas round-trip)", async () => {
    let sentCanvas: { nodes?: Array<{ data?: { l3?: unknown } }> } | undefined;
    server.use(
      http.put(`/api/cabling/topologies/${TOPO_ID}`, async ({ request }) => {
        const body = (await request.json()) as { canvas_data?: typeof sentCanvas };
        sentCanvas = body.canvas_data;
        return HttpResponse.json(PARENT_TOPOLOGY);
      }),
    );
    await renderPageWithCanvas([l3Node("n1", "d-1", [route()])]);

    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(sentCanvas).toBeDefined());

    expect(sentCanvas?.nodes?.[0]?.data?.l3).toEqual({ routes: [route()] });
  });

  it("does not call validate after a save when the canvas carries no data.l3", async () => {
    let validateHit = false;
    server.use(
      http.put(`/api/cabling/topologies/${TOPO_ID}`, () => HttpResponse.json(PARENT_TOPOLOGY)),
      http.post(`/api/cabling/topologies/${TOPO_ID}/validate`, () => {
        validateHit = true;
        return HttpResponse.json({ valid: true, invalid_edges: [], device_ids: [], invalid_routes: [] });
      }),
    );
    await renderPageWithCanvas([deviceNode("n1", "d-1")]);

    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith("Topology saved"));

    expect(validateHit).toBe(false);
  });

  it("calls validate after a save when the canvas carries data.l3, and toasts the problem count with device[index] reason labels", async () => {
    let validateHit = false;
    server.use(
      http.put(`/api/cabling/topologies/${TOPO_ID}`, () => HttpResponse.json(PARENT_TOPOLOGY)),
      http.post(`/api/cabling/topologies/${TOPO_ID}/validate`, () => {
        validateHit = true;
        return HttpResponse.json({
          valid: false,
          invalid_edges: [],
          device_ids: [],
          invalid_routes: [
            { node_id: "n1", device_id: "d-1", index: 0, reason: "l3_bad_destination", detail: null },
          ],
        });
      }),
    );
    await renderPageWithCanvas([l3Node("n1", "d-1", [route()])]);

    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(validateHit).toBe(true));

    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith(
        "Routing intent has 1 problem: d-1[0] l3_bad_destination",
      ),
    );
  });

  it("marks the invalid node's l3ValidationInvalid render flag after a validate finds a problem (E4 badge overlay)", async () => {
    server.use(
      http.put(`/api/cabling/topologies/${TOPO_ID}`, () => HttpResponse.json(PARENT_TOPOLOGY)),
      http.post(`/api/cabling/topologies/${TOPO_ID}/validate`, () =>
        HttpResponse.json({
          valid: false,
          invalid_edges: [],
          device_ids: [],
          invalid_routes: [
            { node_id: "n1", device_id: "d-1", index: null, reason: "l3_switch_unattached", detail: null },
          ],
        }),
      ),
    );
    await renderPageWithCanvas([l3Node("n1", "d-1", [route()])]);

    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(toastError).toHaveBeenCalled());

    const renderedNodes = rfProps.current?.nodes as Array<{ id: string; data: DeviceNodeData }>;
    const renderedNode = renderedNodes.find((n) => n.id === "n1");
    expect(renderedNode?.data.l3ValidationInvalid).toBe(true);
  });

  it("opens the Routing panel only for a single-selected Layer 3 Switch node, and shows the per-row reason line (E3/E5)", async () => {
    server.use(
      http.put(`/api/cabling/topologies/${TOPO_ID}`, () => HttpResponse.json(PARENT_TOPOLOGY)),
      http.post(`/api/cabling/topologies/${TOPO_ID}/validate`, () =>
        HttpResponse.json({
          valid: false,
          invalid_edges: [],
          device_ids: [],
          invalid_routes: [
            { node_id: "n1", device_id: "d-1", index: 0, reason: "l3_unknown_interface", detail: null },
          ],
        }),
      ),
    );
    await renderPageWithCanvas([l3Node("n1", "d-1", [route()], true)]);
    // No panel for a non-L3 device even when selected.
    act(() => {
      useTopologyStore.setState({
        nodes: [deviceNode("n2", "d-2"), l3Node("n1", "d-1", [route()])],
        edges: [],
        selectedEdgeLayer: "L2",
      });
      useTopologyStore.setState((state) => ({
        nodes: state.nodes.map((n) => (n.id === "n2" ? { ...n, selected: true } : n)),
      }));
    });
    expect(screen.queryByText("Routing")).toBeNull();

    // Selecting the L3 switch node opens it.
    act(() => {
      useTopologyStore.setState((state) => ({
        nodes: state.nodes.map((n) => ({ ...n, selected: n.id === "n1" })),
      }));
    });
    expect(await screen.findByText("Routing")).toBeTruthy();
    expect(screen.getByText("d-1")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(toastError).toHaveBeenCalled());
    expect(await screen.findByText("l3_unknown_interface")).toBeTruthy();
  });

  // Review fix F1 (issue #34): l3_duplicate_route is informational, not a
  // problem cabling's own save-gate refuses on; a validate result naming
  // only that reason must not toast an error or turn the badge red.
  it("does not toast an error or mark the badge invalid for a duplicate-only validate result", async () => {
    server.use(
      http.put(`/api/cabling/topologies/${TOPO_ID}`, () => HttpResponse.json(PARENT_TOPOLOGY)),
      http.post(`/api/cabling/topologies/${TOPO_ID}/validate`, () =>
        HttpResponse.json({
          valid: true,
          invalid_edges: [],
          device_ids: [],
          invalid_routes: [
            { node_id: "n1", device_id: "d-1", index: 0, reason: "l3_duplicate_route", detail: null },
          ],
        }),
      ),
    );
    await renderPageWithCanvas([l3Node("n1", "d-1", [route(), route()])]);

    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith("Topology saved"));

    expect(toastError).not.toHaveBeenCalled();
    const renderedNodes = rfProps.current?.nodes as Array<{ id: string; data: DeviceNodeData }>;
    const renderedNode = renderedNodes.find((n) => n.id === "n1");
    expect(renderedNode?.data.l3ValidationInvalid).toBeUndefined();
  });

  // Review fix F5 (issue #34): the reservations gate validates the
  // PERSISTED canvas, so unsaved routing edits are invisible to it; rather
  // than block Reserve, the modal opening warns once when the canvas is
  // dirty and carries L3 intent.
  it("warns on opening the Reserve modal when the canvas has unsaved routing changes", async () => {
    await renderPageWithCanvas([l3Node("n1", "d-1", [route()])]);

    fireEvent.click(screen.getByRole("button", { name: /Reserve Topology/ }));

    await waitFor(() =>
      expect(toastSuccess).toHaveBeenCalledWith(
        "Unsaved routing changes are not checked until you save",
      ),
    );
  });

  it("does not warn on opening the Reserve modal when the canvas has no L3 intent", async () => {
    await renderPageWithCanvas([deviceNode("n1", "d-1")]);

    fireEvent.click(screen.getByRole("button", { name: /Reserve Topology/ }));

    await waitFor(() => expect(screen.getByLabelText("Purpose (optional)")).toBeTruthy());
    expect(toastSuccess).not.toHaveBeenCalledWith(
      "Unsaved routing changes are not checked until you save",
    );
  });
});
