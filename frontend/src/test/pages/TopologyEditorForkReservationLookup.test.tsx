import { http, HttpResponse } from "msw";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import type { ComponentProps } from "react";

// Issue #1319: live edit reads its reservation by id. Before the fix it
// scanned the owner's first 500 reservations; a reservation past that page
// left the editor without it, and Commit computed the device-set union from
// an empty set, dropping the devices not yet removed from the canvas before
// the fork save. These tests pin the read and the fail-closed commit.

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

vi.mock("@xyflow/react", async () => {
  const actual = await vi.importActual<typeof import("@xyflow/react")>("@xyflow/react");
  return {
    ...actual,
    ReactFlow: (props: { children?: React.ReactNode }) => (
      <div data-testid="react-flow">{props.children}</div>
    ),
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

// The real bar, plus a test-only control that calls onCommit even when the
// bar's Commit button is disabled: the stand-in for a keyboard or programmatic
// trigger, so the page's own guard in handleCommitToReservation is tested
// apart from the disabled button.
vi.mock("@/components/topology-editor/LiveEditBar", async () => {
  const actual = await vi.importActual<typeof import("@/components/topology-editor/LiveEditBar")>(
    "@/components/topology-editor/LiveEditBar",
  );
  return {
    LiveEditBar: (props: ComponentProps<typeof actual.LiveEditBar>) => (
      <>
        <actual.LiveEditBar {...props} />
        <button type="button" onClick={props.onCommit}>
          force commit
        </button>
      </>
    ),
  };
});

import { server } from "../mocks/server";
import { TopologyEditorPage } from "@/pages/TopologyEditorPage";
import { useTopologyStore } from "@/stores/topologyStore";

const TOPO_ID = "topo-1";
const RES_ID = "res-far";

function forkNode(id: string, deviceId: string) {
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

// The canvas holds d-keep and a newly drawn d-new; d-off is in the
// reservation but already taken off the canvas.
const FORK = {
  id: "fork-1",
  reservation_id: RES_ID,
  parent_topology_id: TOPO_ID,
  parent_version_id: null,
  status: "ACTIVE",
  canvas_data: {
    nodes: [forkNode("keep-node", "d-keep"), forkNode("new-node", "d-new")],
    edges: [],
    selectedEdgeLayer: "L2",
  },
  created_at: "2026-06-01T00:00:00Z",
  updated_at: "2026-06-01T00:00:00Z",
  connections: [],
  versions: [
    { id: "fv-1", fork_id: "fork-1", version_number: 1, restored_from_id: null, created_at: "2026-06-01T00:00:00Z" },
  ],
  draft_restored_from_id: null,
};

const PARENT_TOPOLOGY = {
  id: TOPO_ID,
  name: "Parent topology",
  created_by: "u",
  owner_name: "u",
  created_at: "2026-05-01T00:00:00Z",
  updated_at: "2026-05-01T00:00:00Z",
  canvas_data: { nodes: [], edges: [], selectedEdgeLayer: "L2" },
};

const RESERVATION = {
  id: RES_ID,
  user_id: "u",
  owner_name: "u",
  device_ids: ["d-keep", "d-off"],
  topology_id: TOPO_ID,
  topology_type: "PHYSICAL",
  purpose: "past the first page",
  start_time: "2026-06-01T00:00:00Z",
  end_time: "2026-06-02T00:00:00Z",
  status: "ACTIVE",
  created_at: "2020-01-01T00:00:00Z",
};

// The owner's list as the old scan saw it: 500 newer reservations on page 0,
// none of them the one being edited.
const NEWER = Array.from({ length: 500 }, (_, i) => ({ ...RESERVATION, id: `newer-${i}` }));

interface Traffic {
  listReads: number;
  patches: Array<{ device_ids: string[] }>;
  calls: string[];
}

function handlers(
  traffic: Traffic,
  reservationRead: () => Response | Promise<Response>,
) {
  return [
    http.get(`/api/reservations/${RES_ID}/fork`, () => HttpResponse.json(FORK)),
    http.get(`/api/cabling/topologies/${TOPO_ID}`, () => HttpResponse.json(PARENT_TOPOLOGY)),
    http.get("/api/ai/status", () => HttpResponse.json({ enabled: false })),
    http.get("/api/reservations/", () => {
      traffic.listReads += 1;
      return HttpResponse.json({ items: NEWER, total: 501, skip: 0, limit: 500 });
    }),
    http.get(`/api/reservations/${RES_ID}`, reservationRead),
    http.patch(`/api/reservations/${RES_ID}`, async ({ request }) => {
      traffic.calls.push("patch");
      traffic.patches.push((await request.json()) as { device_ids: string[] });
      return HttpResponse.json(RESERVATION);
    }),
    http.post(`/api/reservations/${RES_ID}/fork/save`, () => {
      traffic.calls.push("save");
      return HttpResponse.json({
        fork_id: "fork-1",
        version_number: 2,
        released: [],
        built: [],
        unchanged_count: 0,
      });
    }),
  ];
}

function newTraffic(): Traffic {
  return { listReads: 0, patches: [], calls: [] };
}

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[`/topology/${TOPO_ID}?reservationId=${RES_ID}`]}>
        <Routes>
          <Route path="/topology/:id" element={<TopologyEditorPage />} />
          <Route path="/reservations" element={<div>reservations page</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return client;
}

async function canvasLoaded() {
  await waitFor(() =>
    expect(useTopologyStore.getState().nodes.map((n) => n.id)).toContain("new-node"),
  );
}

beforeAll(() => {
  HTMLDialogElement.prototype.showModal = vi.fn();
  HTMLDialogElement.prototype.close = vi.fn();
});

beforeEach(() => {
  useTopologyStore.setState({ nodes: [], edges: [], selectedEdgeLayer: "L2" });
});

afterEach(() => {
  vi.clearAllMocks();
  useTopologyStore.setState({ nodes: [], edges: [], selectedEdgeLayer: "L2" });
});

describe("live edit reads its reservation by id (issue #1319)", () => {
  it("a reservation past the owner's first 500 still drives the header, Restore, and the device-set union", async () => {
    const traffic = newTraffic();
    server.use(...handlers(traffic, () => HttpResponse.json(RESERVATION)));
    renderPage();
    await canvasLoaded();

    expect(await screen.findByText("Editing reservation (past the first page)")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "History" }));
    expect(screen.getByRole("button", { name: "Restore" })).toBeInTheDocument();

    fireEvent.click(await screen.findByRole("button", { name: "Commit to reservation" }));
    await waitFor(() => expect(traffic.calls).toEqual(["patch", "save", "patch"]));
    // The pre-save add keeps d-off, which is off the canvas but not yet
    // removed: dropping it before a save that may fail is the defect.
    expect(new Set(traffic.patches[0].device_ids)).toEqual(new Set(["d-keep", "d-off", "d-new"]));
    // Only after the save is durable does the set settle to the canvas's.
    expect(new Set(traffic.patches[1].device_ids)).toEqual(new Set(["d-keep", "d-new"]));
    expect(traffic.listReads).toBe(0);
  });

  it("a failed read keeps Commit closed and sends neither the PATCH nor the fork save", async () => {
    const traffic = newTraffic();
    server.use(
      ...handlers(traffic, () =>
        HttpResponse.json({ detail: "Reservations service unavailable" }, { status: 503 }),
      ),
    );
    renderPage();
    await canvasLoaded();

    expect((await screen.findByRole("alert")).textContent).toBe(
      "Could not load this reservation, so committing is disabled. Reload the page to try again.",
    );
    const commit = screen.getByRole("button", { name: "Commit to reservation" });
    expect(commit).toBeDisabled();
    fireEvent.click(commit);

    // The page's own guard, reached past the disabled button.
    fireEvent.click(screen.getByRole("button", { name: "force commit" }));
    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith(
        "Could not load this reservation, so nothing was committed; reload the page to try again",
      ),
    );
    expect(traffic.calls).toEqual([]);
    expect(traffic.listReads).toBe(0);
  });

  it("a reservation the caller may not read (404 for anyone but the owner) fails closed and says only the owner can commit", async () => {
    const traffic = newTraffic();
    server.use(
      ...handlers(traffic, () =>
        HttpResponse.json({ detail: "Reservation not found" }, { status: 404 }),
      ),
    );
    renderPage();
    await canvasLoaded();

    // Reloading can never help a non-owner, so the line gives no reload advice.
    expect((await screen.findByRole("alert")).textContent).toBe(
      "Only the reservation's owner can commit from this editor",
    );
    expect(screen.getByRole("button", { name: "Commit to reservation" })).toBeDisabled();
    // No purpose to show, and Restore stays hidden without a known ACTIVE status.
    expect(screen.getByText("Editing reservation")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "History" }));
    expect(screen.queryByRole("button", { name: "Restore" })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "force commit" }));
    await waitFor(() => expect(toastError).toHaveBeenCalled());
    expect(toastError.mock.calls).toEqual([
      ["Only the reservation's owner can commit from this editor"],
    ]);
    expect(traffic.calls).toEqual([]);
  });

  it("a cached reservation stays loaded when a background refetch fails, and Commit unions with the cached set", async () => {
    const traffic = newTraffic();
    server.use(...handlers(traffic, () => HttpResponse.json(RESERVATION)));
    const client = renderPage();
    await canvasLoaded();
    await screen.findByText("Editing reservation (past the first page)");

    // The next read of the reservation fails; refetch only that query.
    server.use(
      http.get(`/api/reservations/${RES_ID}`, () =>
        HttpResponse.json({ detail: "Reservations service unavailable" }, { status: 503 }),
      ),
    );
    await client.invalidateQueries({ queryKey: ["reservations", RES_ID], exact: true });
    await waitFor(() =>
      expect(client.getQueryState(["reservations", RES_ID])?.status).toBe("error"),
    );

    // Deliberate (issue #1319 review): the cached copy is a complete set, so
    // the editor keeps it rather than closing Commit; the server still refuses
    // a reservation that has left ACTIVE.
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByText("Editing reservation (past the first page)")).toBeInTheDocument();
    const commit = screen.getByRole("button", { name: "Commit to reservation" });
    expect(commit).toBeEnabled();
    fireEvent.click(commit);
    await waitFor(() => expect(traffic.calls).toEqual(["patch", "save", "patch"]));
    expect(new Set(traffic.patches[0].device_ids)).toEqual(new Set(["d-keep", "d-off", "d-new"]));
  });

  it("while the read is in flight Commit waits for it, then commits with the full set", async () => {
    const traffic = newTraffic();
    let release: () => void = () => {};
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    server.use(
      ...handlers(traffic, async () => {
        await gate;
        return HttpResponse.json(RESERVATION);
      }),
    );
    renderPage();
    await canvasLoaded();

    expect(await screen.findByRole("button", { name: "Loading reservation..." })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "force commit" }));
    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith(
        "The reservation is still loading; try again in a moment",
      ),
    );
    expect(traffic.calls).toEqual([]);

    release();
    fireEvent.click(await screen.findByRole("button", { name: "Commit to reservation" }));
    await waitFor(() => expect(traffic.calls).toEqual(["patch", "save", "patch"]));
    expect(new Set(traffic.patches[0].device_ids)).toEqual(new Set(["d-keep", "d-off", "d-new"]));
  });
});
