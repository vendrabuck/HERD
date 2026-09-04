import { http, HttpResponse } from "msw";
import { render, screen, waitFor, fireEvent, act, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import type { Node } from "@xyflow/react";

// Pins issue #627's fix to TopologyEditorPage's handlePreviewVersion: a
// parent-topology version preview now routes through hydrateAndLoadCanvas
// (not a raw loadCanvas), and both the eventual loadCanvas call and the
// previewVersion flip are guarded by a request token, so a stale,
// superseded, or exited preview's late hydration can never clobber the
// store or the preview banner.

const { toastError } = vi.hoisted(() => ({
  toastError: vi.fn(),
}));
vi.mock("react-hot-toast", () => ({
  default: Object.assign((msg: string) => msg, {
    error: toastError,
    success: vi.fn(),
    custom: vi.fn(),
    dismiss: vi.fn(),
  }),
}));

vi.mock("@xyflow/react", async () => {
  const actual = await vi.importActual<typeof import("@xyflow/react")>("@xyflow/react");
  return {
    ...actual,
    ReactFlow: (props: Record<string, unknown> & { children?: React.ReactNode }) => (
      <div data-testid="react-flow">{props.children as React.ReactNode}</div>
    ),
    Background: () => <div data-testid="rf-background" />,
    Controls: () => <div data-testid="rf-controls" />,
    MiniMap: () => <div data-testid="rf-minimap" />,
  };
});

vi.mock("@/components/equipment-browser/EquipmentBrowser", () => ({
  EquipmentBrowser: () => <div data-testid="equipment-browser" />,
}));

// Mocked directly (not the lower-level hydrateCanvasNodes), so the
// "hydrated" output can be made observably different from the raw ghosted
// canvas handlePreviewVersion builds, and so tests can control exactly when
// a given preview's hydration resolves.
const { hydrateAndLoadCanvasMock } = vi.hoisted(() => ({
  hydrateAndLoadCanvasMock: vi.fn(),
}));
vi.mock("@/lib/canvasHydration", () => ({
  hydrateAndLoadCanvas: hydrateAndLoadCanvasMock,
}));

import { server } from "../mocks/server";
import { TopologyEditorPage } from "@/pages/TopologyEditorPage";
import { useTopologyStore } from "@/stores/topologyStore";
import type { CanvasData, CanvasNodeData } from "@/types/topology.types";

const TOPO_ID = "topo-preview-1";

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

// Marks every node as having passed through hydration, so a test can tell
// the store received the hydrated canvas rather than the raw ghosted one.
function markHydrated(canvas: CanvasData): CanvasData {
  return {
    ...canvas,
    nodes: canvas.nodes.map((n) => ({
      ...n,
      data: { ...(n.data as Record<string, unknown>), __hydrated: true },
    })) as unknown as CanvasData["nodes"],
  };
}

function deferred() {
  let resolveFn!: () => void;
  const promise = new Promise<void>((resolve) => {
    resolveFn = resolve;
  });
  return { promise, resolveFn };
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

const VERSION_1 = {
  id: "v-1",
  topology_id: TOPO_ID,
  version_number: 1,
  name: "Parent topology",
  description: "first save",
  created_by: "u",
  author_name: "u",
  created_at: "2026-05-02T00:00:00Z",
  restored_from_id: null,
};

const VERSION_2 = {
  id: "v-2",
  topology_id: TOPO_ID,
  version_number: 2,
  name: "Parent topology",
  description: "second save",
  created_by: "u",
  author_name: "u",
  created_at: "2026-05-03T00:00:00Z",
  restored_from_id: null,
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
      HttpResponse.json({ items: [VERSION_2, VERSION_1], total: 2, skip: 0, limit: 200 }),
    ),
    http.get(`/api/cabling/topologies/${TOPO_ID}/versions/v-1`, () =>
      HttpResponse.json({
        ...VERSION_1,
        canvas_data: { nodes: [deviceNode("v1-node", "d-v1")], edges: [], selectedEdgeLayer: "L2" },
      }),
    ),
    http.get(`/api/cabling/topologies/${TOPO_ID}/versions/v-2`, () =>
      HttpResponse.json({
        ...VERSION_2,
        canvas_data: { nodes: [deviceNode("v2-node", "d-v2")], edges: [], selectedEdgeLayer: "L2" },
      }),
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

async function openHistory() {
  fireEvent.click(screen.getByRole("button", { name: "History" }));
  await screen.findByText("Version history");
}

async function clickView(label: string) {
  await screen.findByText(label);
  const row = screen.getByText(label).closest("div.px-4") as HTMLElement;
  fireEvent.click(within(row).getByRole("button", { name: "View" }));
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
  hydrateAndLoadCanvasMock.mockReset();
  hydrateAndLoadCanvasMock.mockImplementation(
    (canvas: CanvasData, cb: (d: CanvasData) => void) => {
      cb(markHydrated(canvas));
      return Promise.resolve();
    },
  );
  toastError.mockClear();
});

afterEach(() => {
  vi.clearAllMocks();
  useTopologyStore.setState({ nodes: [], edges: [], selectedEdgeLayer: "L2" });
});

describe("TopologyEditorPage parent-topology preview hydration (issue #627)", () => {
  it("previews a version through hydrateAndLoadCanvas: the ghosted canvas goes in, the hydrated canvas lands in the store", async () => {
    server.use(...baseHandlers());
    await renderPageWithCanvas([deviceNode("n-live", "d-live")]);

    await openHistory();
    await clickView("v1");

    await waitFor(() => expect(hydrateAndLoadCanvasMock).toHaveBeenCalledTimes(1));
    const [ghosted] = hydrateAndLoadCanvasMock.mock.calls[0] as [CanvasData, unknown];
    expect(ghosted.nodes.map((n) => n.id)).toEqual(["v1-node"]);
    expect(
      ghosted.nodes.every((n) => (n.data as { isProposal?: boolean }).isProposal === true),
    ).toBe(true);

    await waitFor(() =>
      expect(useTopologyStore.getState().nodes.map((n) => n.id)).toContain("v1-node"),
    );
    // The store holds the HYDRATED canvas (the mock's marker), not the raw
    // ghosted one handlePreviewVersion built.
    expect(
      useTopologyStore
        .getState()
        .nodes.every((n) => (n.data as { __hydrated?: boolean }).__hydrated === true),
    ).toBe(true);
    expect(screen.getByRole("button", { name: /Exit preview \(v1\)/ })).toBeInTheDocument();
  });

  it("discards a late hydration result once Exit preview has already run", async () => {
    server.use(...baseHandlers());
    await renderPageWithCanvas([deviceNode("n-live", "d-live")]);

    // Let a first preview complete fully, so its Exit button is on screen
    // while a second preview's hydration is still pending.
    await openHistory();
    await clickView("v1");
    await waitFor(() =>
      expect(useTopologyStore.getState().nodes.map((n) => n.id)).toContain("v1-node"),
    );
    await screen.findByRole("button", { name: /Exit preview \(v1\)/ });

    const pending = deferred();
    hydrateAndLoadCanvasMock.mockImplementationOnce(
      (canvas: CanvasData, cb: (d: CanvasData) => void) =>
        pending.promise.then(() => cb(markHydrated(canvas))),
    );
    await clickView("v2");
    await waitFor(() => expect(hydrateAndLoadCanvasMock).toHaveBeenCalledTimes(2));

    // Exit while v2's hydration is still in flight. previewVersion still
    // names v1 (it is only flipped to v2 once v2's hydration resolves), so
    // the Exit button is the still-mounted v1 one.
    fireEvent.click(screen.getByRole("button", { name: /Exit preview \(v1\)/ }));
    await waitFor(() =>
      expect(useTopologyStore.getState().nodes.map((n) => n.id)).toContain("n-live"),
    );
    expect(screen.queryByRole("button", { name: /Exit preview/ })).not.toBeInTheDocument();

    // Now let v2's hydration resolve. Its result, and the previewVersion
    // flip that would have followed it, must both be discarded: exit
    // already bumped the request token past it.
    await act(async () => {
      pending.resolveFn();
      await pending.promise;
    });
    expect(useTopologyStore.getState().nodes.map((n) => n.id)).toContain("n-live");
    expect(useTopologyStore.getState().nodes.map((n) => n.id)).not.toContain("v2-node");
    expect(screen.queryByRole("button", { name: /Exit preview/ })).not.toBeInTheDocument();
  });

  it("two previews in quick succession: the first's late hydration is ignored, the second's canvas wins", async () => {
    server.use(...baseHandlers());
    await renderPageWithCanvas([deviceNode("n-live", "d-live")]);

    const firstPending = deferred();
    hydrateAndLoadCanvasMock.mockImplementationOnce(
      (canvas: CanvasData, cb: (d: CanvasData) => void) =>
        firstPending.promise.then(() => cb(markHydrated(canvas))),
    );
    await openHistory();
    await clickView("v1");
    await waitFor(() => expect(hydrateAndLoadCanvasMock).toHaveBeenCalledTimes(1));

    // Start a second preview before the first's hydration has resolved. Its
    // own hydration is left on the default (immediate) mock implementation,
    // so it resolves first even though it was requested second.
    await clickView("v2");
    await waitFor(() =>
      expect(useTopologyStore.getState().nodes.map((n) => n.id)).toContain("v2-node"),
    );
    await screen.findByRole("button", { name: /Exit preview \(v2\)/ });

    // Now the first (superseded) preview's hydration finally resolves.
    await act(async () => {
      firstPending.resolveFn();
      await firstPending.promise;
    });

    // Its result must be ignored: the store still shows v2, and the preview
    // banner still names v2, not v1.
    expect(useTopologyStore.getState().nodes.map((n) => n.id)).toContain("v2-node");
    expect(useTopologyStore.getState().nodes.map((n) => n.id)).not.toContain("v1-node");
    expect(screen.getByRole("button", { name: /Exit preview \(v2\)/ })).toBeInTheDocument();
  });
});
