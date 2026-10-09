// Issue #1066: the fork canvas PUT's `invalid_edges` feeds the editor's red
// edge state. The autosave hook is replaced by a capture so each test can
// hand the page a PUT answer directly; the hook's own sequencing rules are
// pinned in src/test/hooks/useForkAutosave.test.tsx.
import { http, HttpResponse } from "msw";
import { act, render, screen, waitFor, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import type { ForkCanvasDraftResult } from "@/types/reservation.types";
import type { CanvasData } from "@/types/topology.types";

const { toastError, toastSuccess, toastCustom, toastDismiss } = vi.hoisted(() => ({
  toastError: vi.fn(),
  toastSuccess: vi.fn(),
  toastCustom: vi.fn(),
  toastDismiss: vi.fn(),
}));
vi.mock("react-hot-toast", () => ({
  default: Object.assign((msg: string) => toastSuccess(msg), {
    error: toastError,
    success: toastSuccess,
    custom: toastCustom,
    dismiss: toastDismiss,
  }),
}));

const autosaveCapture = vi.hoisted(() => ({
  onDraftValidated: null as null | ((r: ForkCanvasDraftResult) => void),
  canvases: [] as CanvasData[],
}));
vi.mock("@/hooks/useForkAutosave", () => ({
  FORK_AUTOSAVE_DELAY_MS: 2000,
  useForkAutosave: (params: {
    canvas: CanvasData;
    onDraftValidated?: (r: ForkCanvasDraftResult) => void;
  }) => {
    autosaveCapture.onDraftValidated = params.onDraftValidated ?? null;
    autosaveCapture.canvases.push(params.canvas);
    return { status: "idle", markClean: () => {}, flush: () => {} };
  },
}));

const rfProps = { current: null as Record<string, unknown> | null };
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
vi.mock("@/components/topology-editor/nodes/DeviceNode", () => ({ DeviceNode: () => null }));
vi.mock("@/components/topology-editor/edges/LayerEdge", () => ({ LayerEdge: () => null }));

import { server } from "../mocks/server";
import { TopologyEditorPage } from "@/pages/TopologyEditorPage";
import { useTopologyStore } from "@/stores/topologyStore";

const TOPO_ID = "topo-1";
const RES_ID = "res-1";

function deviceNode(id: string, deviceId: string) {
  return {
    id,
    type: "deviceNode",
    position: { x: 0, y: 0 },
    data: {
      device: { id: deviceId, name: deviceId, topology_type: "PHYSICAL" },
      label: deviceId,
      topologyType: "PHYSICAL",
    },
  };
}

function line(id: string, source: string, target: string, ports: [string, string]) {
  return {
    id,
    source,
    target,
    type: "layerEdge",
    data: { layer: "L1", source_port_name: ports[0], target_port_name: ports[1] },
  };
}

// Two lines on one device pair (a bundle) plus one line on another pair.
const CANVAS = {
  nodes: [deviceNode("n-a", "d-a"), deviceNode("n-b", "d-b"), deviceNode("n-c", "d-c")],
  edges: [
    line("e-cabled", "n-a", "n-b", ["a1", "b1"]),
    line("e-unjoined", "n-a", "n-b", ["a2", "b2"]),
    line("e-single", "n-b", "n-c", ["b3", "c1"]),
  ],
  selectedEdgeLayer: "L1",
};

function makeFork(canvas: Record<string, unknown> = CANVAS) {
  return {
    id: "fork-1",
    reservation_id: RES_ID,
    parent_topology_id: TOPO_ID,
    parent_version_id: null,
    status: "ACTIVE",
    canvas_data: canvas,
    created_at: "2026-06-01T00:00:00Z",
    updated_at: "2026-06-01T00:00:00Z",
    connections: [],
    versions: [
      {
        id: "fv-1",
        fork_id: "fork-1",
        version_number: 1,
        restored_from_id: null,
        created_at: "2026-06-01T00:00:00Z",
      },
    ],
    draft_restored_from_id: null,
  };
}

const RESERVATION = {
  id: RES_ID,
  user_id: "u",
  owner_name: "u",
  device_ids: ["d-a", "d-b", "d-c"],
  topology_id: TOPO_ID,
  topology_type: "PHYSICAL",
  purpose: "fork test",
  start_time: "2026-06-01T00:00:00Z",
  end_time: "2026-06-02T00:00:00Z",
  status: "ACTIVE",
  created_at: "2026-05-01T00:00:00Z",
};

function baseHandlers() {
  return [
    http.get(`/api/reservations/${RES_ID}/fork`, () => HttpResponse.json(makeFork())),
    http.get(`/api/cabling/topologies/${TOPO_ID}`, () =>
      HttpResponse.json({
        id: TOPO_ID,
        name: "Parent",
        created_by: "u",
        owner_name: "u",
        created_at: "2026-05-01T00:00:00Z",
        updated_at: "2026-05-01T00:00:00Z",
        canvas_data: CANVAS,
      }),
    ),
    http.get("/api/reservations/", () =>
      HttpResponse.json({ items: [RESERVATION], total: 1, skip: 0, limit: 500 }),
    ),
    http.get("/api/ai/status", () => HttpResponse.json({ enabled: false })),
    // Every device pair is reachable, so the client checks pass every line.
    http.post("/api/cabling/pathfind/batch", async ({ request }) => {
      const body = (await request.json()) as {
        pairs: { source_device_id: string; target_device_id: string }[];
      };
      return HttpResponse.json({
        results: body.pairs.map((p) => ({ ...p, reachable: true, hop_count: 1, path: [] })),
      });
    }),
  ];
}

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[`/topology/${TOPO_ID}?reservationId=${RES_ID}`]}>
        <Routes>
          <Route path="/topology/:id" element={<TopologyEditorPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

type RenderEdge = {
  id: string;
  type?: string;
  data?: Record<string, unknown> & {
    members?: Array<{ id: string; data?: Record<string, unknown> }>;
  };
};

// The reason React Flow was handed for one real edge id, whether it renders
// alone or as a bundle member.
function renderedReason(edgeId: string): unknown {
  const edges = (rfProps.current?.edges ?? []) as RenderEdge[];
  for (const edge of edges) {
    if (edge.id === edgeId) return edge.data?.serverInvalidReason;
    const member = edge.data?.members?.find((m) => m.id === edgeId);
    if (member) return member.data?.serverInvalidReason;
  }
  throw new Error(`edge ${edgeId} is not rendered`);
}

function bundleRedByServer(): boolean {
  const edges = (rfProps.current?.edges ?? []) as RenderEdge[];
  const bundle = edges.find((e) => e.type === "bundledEdge");
  return !!bundle?.data?.members?.some((m) => m.data?.serverInvalidReason);
}

async function loadEditor() {
  renderPage();
  await waitFor(() =>
    expect(useTopologyStore.getState().edges.map((e) => e.id)).toContain("e-unjoined"),
  );
  await waitFor(() => expect(autosaveCapture.onDraftValidated).not.toBeNull());
}

function answerDraft(invalidEdges: unknown[]) {
  act(() => {
    autosaveCapture.onDraftValidated?.({
      id: "fork-1",
      valid: invalidEdges.length === 0,
      invalid_edges: invalidEdges,
    });
  });
}

const UNJOINED = {
  edge_id: "e-unjoined",
  source_device_id: "d-a",
  target_device_id: "d-b",
  layer: "L1",
  reason: "no_port_path",
};

beforeAll(() => {
  HTMLDialogElement.prototype.showModal = vi.fn();
  HTMLDialogElement.prototype.close = vi.fn();
});

beforeEach(() => {
  useTopologyStore.setState({ nodes: [], edges: [], selectedEdgeLayer: "L2" });
  autosaveCapture.onDraftValidated = null;
  autosaveCapture.canvases = [];
});

afterEach(() => {
  vi.clearAllMocks();
  rfProps.current = null;
  useTopologyStore.setState({ nodes: [], edges: [], selectedEdgeLayer: "L2" });
});

describe("TopologyEditorPage fork server edge verdicts (issue #1066)", () => {
  it("paints a line the draft PUT reported, inside its bundle, without touching the store", async () => {
    server.use(...baseHandlers());
    await loadEditor();
    expect(renderedReason("e-unjoined")).toBeUndefined();

    answerDraft([UNJOINED]);

    await waitFor(() => expect(renderedReason("e-unjoined")).toBe("no_port_path"));
    expect(bundleRedByServer()).toBe(true);
    expect(renderedReason("e-cabled")).toBeUndefined();
    expect(renderedReason("e-single")).toBeUndefined();
    // Render-only: the store and every canvas handed to the autosave stay clean.
    expect(
      useTopologyStore.getState().edges.some((e) => e.data && "serverInvalidReason" in e.data),
    ).toBe(false);
    for (const canvas of autosaveCapture.canvases) {
      expect(canvas.edges.some((e) => e.data && "serverInvalidReason" in e.data)).toBe(false);
    }
    expect(
      screen.getByText(
        "1 line failed the last draft check; committing does not wire it. Hover its label for the reason.",
      ),
    ).toBeInTheDocument();
    // Not a commit block: the save wires the rest (issue #1007).
    expect(screen.getByRole("button", { name: "Commit to reservation" })).toBeEnabled();
  });

  it("marks an unbundled line directly", async () => {
    server.use(...baseHandlers());
    await loadEditor();
    answerDraft([{ ...UNJOINED, edge_id: "e-single" }]);
    await waitFor(() => expect(renderedReason("e-single")).toBe("no_port_path"));
    expect(bundleRedByServer()).toBe(false);
  });

  it("stays red until the next PUT answer says otherwise", async () => {
    server.use(...baseHandlers());
    await loadEditor();
    answerDraft([UNJOINED]);
    await waitFor(() => expect(renderedReason("e-unjoined")).toBe("no_port_path"));

    answerDraft([]);
    await waitFor(() => expect(renderedReason("e-unjoined")).toBeUndefined());
    expect(screen.queryByText(/last draft check/)).not.toBeInTheDocument();
  });

  it("ignores reported ids that are not on the canvas and entries it cannot place", async () => {
    server.use(...baseHandlers());
    await loadEditor();
    answerDraft([{ edge_id: "gone", reason: "no_path" }, { reason: "no_path" }, "junk"]);
    expect(renderedReason("e-unjoined")).toBeUndefined();
    expect(screen.queryByText(/last draft check/)).not.toBeInTheDocument();
  });

  it("clears the verdicts when a fork-history preview replaces the canvas, and they do not return on exit", async () => {
    server.use(
      ...baseHandlers(),
      http.get(`/api/reservations/${RES_ID}/fork/versions/fv-1`, () =>
        HttpResponse.json({
          id: "fv-1",
          fork_id: "fork-1",
          version_number: 1,
          restored_from_id: null,
          created_at: "2026-06-01T00:00:00Z",
          // The version reuses the same edge id, the case the clear exists for.
          canvas_data: CANVAS,
        }),
      ),
    );
    await loadEditor();
    answerDraft([UNJOINED]);
    await waitFor(() => expect(renderedReason("e-unjoined")).toBe("no_port_path"));

    fireEvent.click(screen.getByRole("button", { name: "History" }));
    fireEvent.click(screen.getByRole("button", { name: "Preview" }));
    await waitFor(() => expect(screen.getByText(/Previewing version 1/)).toBeInTheDocument());
    await waitFor(() =>
      expect(useTopologyStore.getState().nodes.every((n) => n.data?.isProposal === true)).toBe(
        true,
      ),
    );
    expect(renderedReason("e-unjoined")).toBeUndefined();

    fireEvent.click(screen.getByRole("button", { name: /Exit preview/ }));
    await waitFor(() => expect(screen.getByText("EDITING LIVE RESERVATION")).toBeInTheDocument());
    expect(renderedReason("e-unjoined")).toBeUndefined();
  });

  it("a fork save refreshes the no_port_path verdicts from its skipped list and keeps the others", async () => {
    let skipped: unknown[] = [];
    server.use(
      ...baseHandlers(),
      http.post(`/api/reservations/${RES_ID}/fork/save`, () =>
        HttpResponse.json({
          fork_id: "fork-1",
          version_number: 2,
          released: [],
          built: [],
          unchanged_count: 1,
          constrained_edges_skipped: skipped,
        }),
      ),
      http.patch(`/api/reservations/${RES_ID}`, () => HttpResponse.json(RESERVATION)),
    );
    await loadEditor();
    answerDraft([UNJOINED, { ...UNJOINED, edge_id: "e-single", reason: "element_edge_no_port" }]);
    await waitFor(() => expect(renderedReason("e-unjoined")).toBe("no_port_path"));

    // The save wired the line after all (a cable landed): its verdict goes,
    // the reason the save does not judge stays.
    fireEvent.click(screen.getByRole("button", { name: "Commit to reservation" }));
    await waitFor(() => expect(toastCustom).toHaveBeenCalled());
    await waitFor(() => expect(renderedReason("e-unjoined")).toBeUndefined());
    expect(renderedReason("e-single")).toBe("element_edge_no_port");

    // A later save that skips the line paints it again.
    skipped = [
      {
        edge_id: "e-unjoined",
        source_device_id: "d-a",
        target_device_id: "d-b",
        source_port_name: "a2",
        target_port_name: "b2",
      },
    ];
    toastCustom.mockClear();
    fireEvent.click(screen.getByRole("button", { name: "Commit to reservation" }));
    await waitFor(() => expect(toastCustom).toHaveBeenCalled());
    await waitFor(() => expect(renderedReason("e-unjoined")).toBe("no_port_path"));
  });
});
