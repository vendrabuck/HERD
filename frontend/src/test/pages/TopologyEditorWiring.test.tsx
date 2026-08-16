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

// Same capture pattern as TopologyEditorDynamicPlaceholders.test.tsx: stub the
// heavy canvas but forward the page's real handlers (onConnect, onEdgesChange,
// onNodesDelete) so the wiring entry-point decision and the bundle-expansion
// safety nets are exercised for real.
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

// The dialog/popover fetch ports and physical-cabling connections per side;
// mock both the same way the component-level dialog tests do, reusing the
// shared fixtures (issue #517 review item 11) for the device/port shape.
import {
  SOURCE_DEVICE,
  TARGET_DEVICE,
  SOURCE_PORTS,
  TARGET_PORTS,
  EMPTY_PORTS,
  EMPTY_CONNECTIONS,
} from "../fixtures/wiringFixtures";

vi.mock("@/api/ports", () => ({
  usePorts: (deviceId: string) => {
    if (deviceId === SOURCE_DEVICE) return { data: SOURCE_PORTS, isLoading: false };
    if (deviceId === TARGET_DEVICE) return { data: TARGET_PORTS, isLoading: false };
    return { data: EMPTY_PORTS, isLoading: false };
  },
}));
vi.mock("@/api/connections", () => ({
  useDeviceConnections: () => ({ data: EMPTY_CONNECTIONS, isLoading: false }),
  usePathfindPairs: () => ({ data: undefined }),
}));

import { server } from "../mocks/server";
import { TopologyEditorPage } from "@/pages/TopologyEditorPage";
import { useTopologyStore } from "@/stores/topologyStore";
import type { CanvasNodeData } from "@/types/topology.types";

const TOPO_ID = "topo-wiring-1";

const PARENT_TOPOLOGY = {
  id: TOPO_ID,
  name: "Parent topology",
  created_by: "u",
  owner_name: "u",
  created_at: "2026-05-01T00:00:00Z",
  updated_at: "2026-05-01T00:00:00Z",
  canvas_data: null,
};

function deviceNode(id: string, deviceId: string, name: string): Node<CanvasNodeData> {
  return {
    id,
    type: "deviceNode",
    position: { x: 0, y: 0 },
    data: {
      device: { id: deviceId, name, topology_type: "PHYSICAL", status: "AVAILABLE" },
      label: name,
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

function drawConnection() {
  const connection = {
    source: "n-src",
    target: "n-tgt",
    sourceHandle: "right",
    targetHandle: "left",
  };
  const onConnect = rfProps.current?.onConnect as (c: typeof connection) => void;
  act(() => onConnect(connection));
}

// The wiring dialog's port rows react to a full mousedown/mouseup gesture
// (no intervening mousemove), not a bare click event (issue #517 review
// item 2's gesture rework: the previous split mousedown/onClick design never
// actually completed a click-to-connect pair in a real browser).
function clickPort(text: string) {
  const row = screen.getByText(text).closest("[data-port-id]") as HTMLElement;
  fireEvent.mouseDown(row);
  fireEvent.mouseUp(row);
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

describe("TopologyEditorPage wiring entry point", () => {
  it("drawing a line opens the full wiring dialog by default (primary post-draw surface)", async () => {
    server.use(...baseHandlers());
    await renderPageWithCanvas([
      deviceNode("n-src", SOURCE_DEVICE, "switch-a"),
      deviceNode("n-tgt", TARGET_DEVICE, "switch-b"),
    ]);

    drawConnection();

    expect(await screen.findByText("Wire switch-a to switch-b")).toBeInTheDocument();
    expect(screen.queryByText("New connection")).not.toBeInTheDocument();
  });

  it("toggling Quick connect opens the compact popover instead, with an escalation link back to the full dialog", async () => {
    server.use(...baseHandlers());
    await renderPageWithCanvas([
      deviceNode("n-src", SOURCE_DEVICE, "switch-a"),
      deviceNode("n-tgt", TARGET_DEVICE, "switch-b"),
    ]);

    fireEvent.click(screen.getByRole("button", { name: "Quick connect" }));
    drawConnection();

    expect(await screen.findByText("New connection")).toBeInTheDocument();
    expect(screen.queryByText("Wire switch-a to switch-b")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Open wiring dialog" }));
    expect(await screen.findByText("Wire switch-a to switch-b")).toBeInTheDocument();
    expect(screen.queryByText("New connection")).not.toBeInTheDocument();
  });

  it("confirming N lines in the wiring dialog adds N edges to the store in a single commit", async () => {
    server.use(...baseHandlers());
    await renderPageWithCanvas([
      deviceNode("n-src", SOURCE_DEVICE, "switch-a"),
      deviceNode("n-tgt", TARGET_DEVICE, "switch-b"),
    ]);

    drawConnection();
    await screen.findByText("Wire switch-a to switch-b");

    clickPort("eth1");
    clickPort("0/0/1");
    clickPort("eth2");
    clickPort("0/0/2");

    fireEvent.click(screen.getByRole("button", { name: "Add 2 connections (keeps 1 per pair)" }));

    await waitFor(() => {
      expect(useTopologyStore.getState().edges).toHaveLength(2);
    });
    const ids = useTopologyStore.getState().edges.map((e) => e.id);
    expect(new Set(ids).size).toBe(2);
    expect(screen.queryByText("Wire switch-a to switch-b")).not.toBeInTheDocument();
  });

  it("cancel adds no edges", async () => {
    server.use(...baseHandlers());
    await renderPageWithCanvas([
      deviceNode("n-src", SOURCE_DEVICE, "switch-a"),
      deviceNode("n-tgt", TARGET_DEVICE, "switch-b"),
    ]);

    drawConnection();
    await screen.findByText("Wire switch-a to switch-b");
    clickPort("eth1");
    clickPort("0/0/1");

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(useTopologyStore.getState().edges).toHaveLength(0);
    expect(screen.queryByText("Wire switch-a to switch-b")).not.toBeInTheDocument();
  });

  it("a second quick-connect line for an already-wired pair is not silently dropped (review item 5)", async () => {
    server.use(...baseHandlers());
    await renderPageWithCanvas([
      deviceNode("n-src", SOURCE_DEVICE, "switch-a"),
      deviceNode("n-tgt", TARGET_DEVICE, "switch-b"),
    ]);
    fireEvent.click(screen.getByRole("button", { name: "Quick connect" }));

    drawConnection();
    await screen.findByText("New connection");
    let [sourceSelect, targetSelect] = screen.getAllByRole("combobox", { hidden: true });
    fireEvent.change(sourceSelect, { target: { value: "sp1" } });
    fireEvent.change(targetSelect, { target: { value: "tp1" } });
    fireEvent.click(screen.getByRole("button", { name: "Connect" }));
    await waitFor(() => expect(useTopologyStore.getState().edges).toHaveLength(1));

    // Same source/target node pair and the same React Flow handles as the
    // first draw: addEnrichedEdge/addEdge's connectionExists guard used to
    // silently refuse this second line.
    drawConnection();
    await screen.findByText("New connection");
    [sourceSelect, targetSelect] = screen.getAllByRole("combobox", { hidden: true });
    fireEvent.change(sourceSelect, { target: { value: "sp2" } });
    fireEvent.change(targetSelect, { target: { value: "tp2" } });
    fireEvent.click(screen.getByRole("button", { name: "Connect" }));

    await waitFor(() => expect(useTopologyStore.getState().edges).toHaveLength(2));
    const ids = useTopologyStore.getState().edges.map((e) => e.id);
    expect(new Set(ids).size).toBe(2);
  });

  it("a port already wired to the counterpart by an EXISTING canvas edge renders WIRED in a fresh dialog session (review item 8)", async () => {
    server.use(...baseHandlers());
    await renderPageWithCanvas([
      deviceNode("n-src", SOURCE_DEVICE, "switch-a"),
      deviceNode("n-tgt", TARGET_DEVICE, "switch-b"),
    ]);
    // An existing canvas edge already wires eth1 to 0/0/1, drawn before this
    // test's fresh dialog session opens.
    act(() => {
      useTopologyStore.setState({
        edges: [
          {
            id: "existing-e1",
            source: "n-src",
            target: "n-tgt",
            type: "layerEdge",
            data: { layer: "L1", source_port_id: "sp1", target_port_id: "tp1" },
          },
        ],
      });
    });

    drawConnection();
    await screen.findByText("Wire switch-a to switch-b");

    const sourceRow = screen.getByTestId("port-row-sp1");
    expect(sourceRow).toHaveAttribute("title", "already connected on the canvas");
    const targetRow = screen.getByTestId("port-row-tp1");
    expect(targetRow).toHaveAttribute("title", "already connected on the canvas");

    // A free pair (eth2/0/0/2) still wires normally.
    clickPort("eth2");
    clickPort("0/0/2");
    fireEvent.click(screen.getByRole("button", { name: "Add 1 connection" }));

    await waitFor(() => expect(useTopologyStore.getState().edges).toHaveLength(2));
  });

  it("a port wired to a THIRD device is canvas-wired too, regardless of who the other end is (review round 3 item 5)", async () => {
    server.use(...baseHandlers());
    await renderPageWithCanvas([
      deviceNode("n-src", SOURCE_DEVICE, "switch-a"),
      deviceNode("n-tgt", TARGET_DEVICE, "switch-b"),
      deviceNode("n-third", "device-third", "switch-c"),
    ]);
    // eth1 (sp1) is already wired from switch-a to a THIRD device, not to
    // switch-b at all. The original (pre-fix) scope only looked at edges
    // between the exact pending pair and would have missed this.
    act(() => {
      useTopologyStore.setState({
        edges: [
          {
            id: "existing-e1",
            source: "n-src",
            target: "n-third",
            type: "layerEdge",
            data: { layer: "L1", source_port_id: "sp1", target_port_id: "some-third-port" },
          },
        ],
      });
    });

    drawConnection();
    await screen.findByText("Wire switch-a to switch-b");

    const sourceRow = screen.getByTestId("port-row-sp1");
    expect(sourceRow).toHaveAttribute("title", "already connected on the canvas");
    expect(sourceRow).toHaveAttribute("data-status", "canvas-wired");
  });

  it("the third-device case is caught in the quick-connect popover too, both surfaces share one derivation (review round 3 item 5)", async () => {
    server.use(...baseHandlers());
    await renderPageWithCanvas([
      deviceNode("n-src", SOURCE_DEVICE, "switch-a"),
      deviceNode("n-tgt", TARGET_DEVICE, "switch-b"),
      deviceNode("n-third", "device-third", "switch-c"),
    ]);
    act(() => {
      useTopologyStore.setState({
        edges: [
          {
            id: "existing-e1",
            source: "n-src",
            target: "n-third",
            type: "layerEdge",
            data: { layer: "L1", source_port_id: "sp1", target_port_id: "some-third-port" },
          },
        ],
      });
    });

    fireEvent.click(screen.getByRole("button", { name: "Quick connect" }));
    drawConnection();
    await screen.findByText("New connection");

    const options = screen.getAllByRole("option", { hidden: true }) as HTMLOptionElement[];
    const eth1Option = options.find((o) => o.value === "sp1")!;
    expect(eth1Option.disabled).toBe(true);
    expect(eth1Option.textContent).toBe("eth1 (already connected)");
  });
});

describe("TopologyEditorPage bundle-safe edge changes (review item 3)", () => {
  it("a remove change on a bundled edge id expands to every member id before reaching the store", async () => {
    server.use(...baseHandlers());
    await renderPageWithCanvas([
      deviceNode("n-src", SOURCE_DEVICE, "switch-a"),
      deviceNode("n-tgt", TARGET_DEVICE, "switch-b"),
    ]);
    act(() => {
      useTopologyStore.setState({
        edges: [
          { id: "e1", source: "n-src", target: "n-tgt", type: "layerEdge", data: { layer: "L1" } },
          { id: "e2", source: "n-src", target: "n-tgt", type: "layerEdge", data: { layer: "L2" } },
        ],
      });
    });

    const renderEdges = rfProps.current?.edges as Array<{ id: string }>;
    expect(renderEdges).toHaveLength(1); // bundled on the canvas
    const bundleId = renderEdges[0].id;

    const onEdgesChange = rfProps.current?.onEdgesChange as (
      changes: Array<{ id: string; type: string }>,
    ) => void;
    act(() => onEdgesChange([{ id: bundleId, type: "remove" }]));

    expect(useTopologyStore.getState().edges).toHaveLength(0);
  });

  it("a replace change targeting a bundle id is NOT expanded, so the synthetic bundle shape never gets written into a real edge slot (review round 3 item 11)", async () => {
    server.use(...baseHandlers());
    await renderPageWithCanvas([
      deviceNode("n-src", SOURCE_DEVICE, "switch-a"),
      deviceNode("n-tgt", TARGET_DEVICE, "switch-b"),
    ]);
    act(() => {
      useTopologyStore.setState({
        edges: [
          { id: "e1", source: "n-src", target: "n-tgt", type: "layerEdge", data: { layer: "L1" } },
          { id: "e2", source: "n-src", target: "n-tgt", type: "layerEdge", data: { layer: "L2" } },
        ],
      });
    });
    const bundleId = (rfProps.current?.edges as Array<{ id: string }>)[0].id;

    const onEdgesChange = rfProps.current?.onEdgesChange as (
      changes: Array<{ id: string; type: string; item?: unknown }>,
    ) => void;
    // The `item` here is exactly the shape React Flow would try to write:
    // the SYNTHETIC bundle object (data.members), never a real per-member
    // edge. If this were expanded per-member, it would corrupt both store
    // edges with that shape.
    act(() =>
      onEdgesChange([
        { id: bundleId, type: "replace", item: { id: bundleId, data: { members: [] } } },
      ]),
    );

    expect(useTopologyStore.getState().edges).toHaveLength(2);
    expect(useTopologyStore.getState().edges.map((e) => e.data)).toEqual([
      { layer: "L1" },
      { layer: "L2" },
    ]);
  });

  it("select, deselect, and Delete on a bundled edge all round-trip through the RENDER view, not just the store (review item 1)", async () => {
    // The root cause of the original item 3 bug: a freshly-built bundle
    // object never echoed `selected` back, so React Flow's controlled
    // reconciliation always saw it as unselected no matter what happened to
    // its members, and once a select change got applied to the store there
    // was no way for React Flow to ever ask for it to be cleared again. This
    // test drives every step through the real onEdgesChange stream (never
    // the store directly) and checks the RENDER view (rfProps.current.edges,
    // what React Flow itself would see) at each step, not just the store.
    server.use(...baseHandlers());
    await renderPageWithCanvas([
      deviceNode("n-src", SOURCE_DEVICE, "switch-a"),
      deviceNode("n-tgt", TARGET_DEVICE, "switch-b"),
    ]);
    act(() => {
      useTopologyStore.setState({
        edges: [
          { id: "e1", source: "n-src", target: "n-tgt", type: "layerEdge", data: { layer: "L1" } },
          { id: "e2", source: "n-src", target: "n-tgt", type: "layerEdge", data: { layer: "L2" } },
        ],
      });
    });
    const bundleId = (rfProps.current?.edges as Array<{ id: string }>)[0].id;

    const onEdgesChange = rfProps.current?.onEdgesChange as (
      changes: Array<{ id: string; type: string; selected?: boolean }>,
    ) => void;

    // 1. Select the bundle: both store members flip, AND the freshly
    // recomputed bundle in the render view reflects it (this is the part
    // that was broken).
    act(() => onEdgesChange([{ id: bundleId, type: "select", selected: true }]));
    expect(useTopologyStore.getState().edges.every((e) => e.selected)).toBe(true);
    const bundleAfterSelect = (rfProps.current?.edges as Array<{ id: string; selected?: boolean }>)[0];
    expect(bundleAfterSelect.id).toBe(bundleId);
    expect(bundleAfterSelect.selected).toBe(true);

    // 2. Deselect the bundle the same way (a second click, in the real UI):
    // both members clear, and the render view reflects that too.
    act(() => onEdgesChange([{ id: bundleId, type: "select", selected: false }]));
    expect(useTopologyStore.getState().edges.every((e) => e.selected === false)).toBe(true);
    const bundleAfterDeselect = (rfProps.current?.edges as Array<{ selected?: boolean }>)[0];
    expect(bundleAfterDeselect.selected).toBe(false);

    // 3. Select again, then Delete: both members are gone, driven entirely
    // through the React Flow change stream.
    act(() => onEdgesChange([{ id: bundleId, type: "select", selected: true }]));
    act(() => onEdgesChange([{ id: bundleId, type: "remove" }]));
    expect(useTopologyStore.getState().edges).toHaveLength(0);
  });

  it("a selected edge never persists its `selected` field into saved canvas_data (review item 1)", async () => {
    let putBody: Record<string, unknown> | null = null;
    server.use(
      ...baseHandlers(),
      http.put(`/api/cabling/topologies/${TOPO_ID}`, async ({ request }) => {
        putBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(PARENT_TOPOLOGY);
      }),
    );
    await renderPageWithCanvas([
      deviceNode("n-src", SOURCE_DEVICE, "switch-a"),
      deviceNode("n-tgt", TARGET_DEVICE, "switch-b"),
    ]);
    act(() => {
      useTopologyStore.setState({
        edges: [{ id: "e1", source: "n-src", target: "n-tgt", type: "layerEdge", data: { layer: "L1" } }],
      });
    });
    const onEdgesChange = rfProps.current?.onEdgesChange as (
      changes: Array<{ id: string; type: string; selected?: boolean }>,
    ) => void;
    act(() => onEdgesChange([{ id: "e1", type: "select", selected: true }]));
    expect(useTopologyStore.getState().edges[0].selected).toBe(true);

    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(putBody).not.toBeNull());

    const canvas = putBody as unknown as { canvas_data: { edges: Array<Record<string, unknown>> } };
    expect(canvas.canvas_data.edges).toHaveLength(1);
    expect("selected" in canvas.canvas_data.edges[0]).toBe(false);
  });

  it("deleting a node removes every store edge incident to it, even ones bundled on the canvas", async () => {
    server.use(...baseHandlers());
    await renderPageWithCanvas([
      deviceNode("n-src", SOURCE_DEVICE, "switch-a"),
      deviceNode("n-tgt", TARGET_DEVICE, "switch-b"),
    ]);
    act(() => {
      useTopologyStore.setState({
        edges: [
          { id: "e1", source: "n-src", target: "n-tgt", type: "layerEdge", data: { layer: "L1" } },
          { id: "e2", source: "n-src", target: "n-tgt", type: "layerEdge", data: { layer: "L2" } },
        ],
      });
    });

    const onNodesDelete = rfProps.current?.onNodesDelete as (
      nodes: Array<{ id: string }>,
    ) => void;
    act(() => onNodesDelete([{ id: "n-src" }]));

    expect(useTopologyStore.getState().edges).toHaveLength(0);
  });
});
