import { StrictMode, useEffect } from "react";
import { http, HttpResponse } from "msw";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useNavigate, type NavigateFunction } from "react-router-dom";
import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

// Issue #1315: the canvas shown and saved for /topology/:id is always topology
// :id's canvas, or an explicit loading or error state. The canvas store is a
// global singleton, so these sequences deliberately NEVER reset it by hand:
// every test starts from whatever the previous one left behind, and every
// sequence opens topology A first so the store holds a foreign canvas when the
// topology under test opens. The other editor suites reset the store in
// beforeEach and afterEach, which is exactly why they never saw the bug.
//
// Hydration is not mocked: POST /inventory/devices/batch goes through MSW and
// can be held open per device id, to reproduce the window between the
// topology GET resolving and the canvas landing in the store.

vi.mock("react-hot-toast", () => ({
  default: Object.assign(() => undefined, {
    error: vi.fn(),
    success: vi.fn(),
    custom: vi.fn(),
    dismiss: vi.fn(),
  }),
}));

// React Flow is replaced by a list of the node ids it was handed, so what the
// canvas shows is readable through a role query.
vi.mock("@xyflow/react", async () => {
  const actual = await vi.importActual<typeof import("@xyflow/react")>("@xyflow/react");
  return {
    ...actual,
    ReactFlow: (props: {
      nodes?: Array<{ id: string }>;
      onDrop?: React.DragEventHandler;
      nodesConnectable?: boolean;
      deleteKeyCode?: string | null;
      children?: React.ReactNode;
    }) => (
      <div
        data-testid="react-flow"
        data-connectable={String(props.nodesConnectable)}
        data-delete-key={String(props.deleteKeyCode)}
        onDrop={props.onDrop}
      >
        <ul aria-label="canvas nodes">
          {(props.nodes ?? []).map((n) => (
            <li key={n.id}>{n.id}</li>
          ))}
        </ul>
        {props.children}
      </div>
    ),
    Background: () => null,
    Controls: () => null,
    MiniMap: () => null,
  };
});

vi.mock("@/components/equipment-browser/EquipmentBrowser", () => ({
  EquipmentBrowser: () => <div data-testid="equipment-browser" />,
}));

import { server } from "../mocks/server";
import { TopologyEditorPage } from "@/pages/TopologyEditorPage";
import { useTopologyStore } from "@/stores/topologyStore";
import { useAuthStore } from "@/stores/authStore";

function deviceNode(id: string, deviceId: string) {
  return {
    id,
    type: "deviceNode",
    position: { x: 0, y: 0 },
    data: {
      device: { id: deviceId, name: deviceId, topology_type: "PHYSICAL", status: "AVAILABLE" },
      label: deviceId,
      topologyType: "PHYSICAL",
    },
  };
}

function topology(id: string, name: string, nodeIds: Array<[string, string]> | null) {
  return {
    id,
    name,
    created_by: "u",
    owner_name: "u",
    created_at: "2026-05-01T00:00:00Z",
    updated_at: "2026-05-01T00:00:00Z",
    canvas_data:
      nodeIds === null
        ? null
        : { nodes: nodeIds.map(([n, d]) => deviceNode(n, d)), edges: [], selectedEdgeLayer: "L2" },
  };
}

const TOPOLOGIES: Record<string, ReturnType<typeof topology>> = {
  A: topology("A", "Topology A", [["a-node", "d-a"]]),
  B: topology("B", "Topology B", [["b-node", "d-b"]]),
  C: topology("C", "Topology C", [["c-node", "d-c"]]),
  // A topology just created from the list page (or committed by the AI
  // dialog): no canvas yet. There is no separate new-topology route; both
  // create flows navigate to /topology/:id of the new row.
  E: topology("E", "Topology E", null),
  T: topology("T", "Topology T", [["parent-node", "d-parent"]]),
};

const RES_ID = "res-1";
const RESERVATION = {
  id: RES_ID,
  user_id: "u",
  owner_name: "u",
  device_ids: ["d-fork"],
  topology_id: "T",
  topology_type: "PHYSICAL",
  purpose: "fork test",
  start_time: "2026-06-01T00:00:00Z",
  end_time: "2026-06-02T00:00:00Z",
  status: "ACTIVE",
  created_at: "2026-05-01T00:00:00Z",
};
const FORK = {
  id: "fork-1",
  reservation_id: RES_ID,
  parent_topology_id: "T",
  parent_version_id: null,
  status: "ACTIVE",
  canvas_data: { nodes: [deviceNode("fork-node", "d-fork")], edges: [], selectedEdgeLayer: "L2" },
  created_at: "2026-06-01T00:00:00Z",
  updated_at: "2026-06-01T00:00:00Z",
  connections: [],
  versions: [],
  draft_restored_from_id: null,
};

const VERSION_A1 = {
  id: "va-1",
  topology_id: "A",
  version_number: 1,
  name: "Topology A",
  description: "first save of A",
  created_by: "u",
  author_name: "u",
  created_at: "2026-05-02T00:00:00Z",
  restored_from_id: null,
};

// Per-device-id holds on the inventory batch call (the hydration step).
const holds = new Map<string, Promise<void>>();
function holdHydration(deviceId: string): () => void {
  let release!: () => void;
  holds.set(deviceId, new Promise<void>((r) => (release = r)));
  return () => {
    holds.delete(deviceId);
    release();
  };
}
// Topology ids whose GET fails, with the status it fails with.
const failures = new Map<string, number>();
let forkHold: Promise<void> | null = null;
let puts: Array<{ id: string; nodeIds: string[] }> = [];

function handlers() {
  return [
    http.get("/api/cabling/topologies/:id", ({ params }) => {
      const id = params.id as string;
      const status = failures.get(id);
      if (status) return HttpResponse.json({ detail: "nope" }, { status });
      return HttpResponse.json(TOPOLOGIES[id]);
    }),
    http.put("/api/cabling/topologies/:id", async ({ params, request }) => {
      const body = (await request.json()) as { canvas_data: { nodes: Array<{ id: string }> } };
      puts.push({ id: params.id as string, nodeIds: body.canvas_data.nodes.map((n) => n.id) });
      return HttpResponse.json(TOPOLOGIES[params.id as string]);
    }),
    http.post("/api/inventory/devices/batch", async ({ request }) => {
      const body = (await request.json()) as { device_ids: string[] };
      for (const d of body.device_ids) {
        const hold = holds.get(d);
        if (hold) await hold;
      }
      return HttpResponse.json({ items: [] });
    }),
    http.get(`/api/reservations/${RES_ID}/fork`, async () => {
      if (forkHold) await forkHold;
      return HttpResponse.json(FORK);
    }),
    http.get("/api/reservations/", () =>
      HttpResponse.json({ items: [RESERVATION], total: 1, skip: 0, limit: 500 }),
    ),
    http.get("/api/ai/status", () => HttpResponse.json({ enabled: false })),
    http.get("/api/cabling/topologies/:id/versions", ({ params }) =>
      HttpResponse.json(
        params.id === "A"
          ? { items: [VERSION_A1], total: 1, skip: 0, limit: 200 }
          : { items: [], total: 0, skip: 0, limit: 200 },
      ),
    ),
    http.get("/api/cabling/topologies/A/versions/va-1", () =>
      HttpResponse.json({
        ...VERSION_A1,
        canvas_data: { nodes: [deviceNode("v1-node", "d-v1")], edges: [], selectedEdgeLayer: "L2" },
      }),
    ),
  ];
}

const navRef: { current: NavigateFunction | null } = { current: null };
const keepNavigate = (navigate: NavigateFunction) => {
  navRef.current = navigate;
};
function NavProbe({ onNavigate }: { onNavigate: (navigate: NavigateFunction) => void }) {
  const navigate = useNavigate();
  useEffect(() => onNavigate(navigate), [navigate, onNavigate]);
  return null;
}

// One QueryClient per sequence, so a revisit within a sequence is a real
// cached revisit (data present at once, refetched in the background).
function renderApp(initial: string, strict = false) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const app = (
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[initial]}>
        <NavProbe onNavigate={keepNavigate} />
        <Routes>
          <Route path="/topology/:id" element={<TopologyEditorPage />} />
          <Route path="/topology" element={<div>topology list page</div>} />
          <Route path="/login" element={<div>login page</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
  return render(strict ? <StrictMode>{app}</StrictMode> : app);
}

async function go(path: string) {
  await act(async () => {
    navRef.current!(path);
  });
}

// What the canvas shows, or null when no canvas is rendered at all.
function canvasNodeIds(): string[] | null {
  const list = screen.queryByRole("list", { name: "canvas nodes" });
  if (!list) return null;
  return Array.from(list.querySelectorAll("li")).map((li) => li.textContent ?? "");
}

function saveButton(): HTMLElement {
  return screen.getByRole("button", { name: "Save" });
}

// Lets every pending fetch, microtask, and effect run, so a "still not
// ready" assertion is not just a race against a slow promise.
async function settle() {
  await act(async () => {
    await new Promise((r) => setTimeout(r, 30));
  });
}

async function openAndWaitReady(name: string, nodeIds: string[]) {
  await screen.findByText(name);
  await waitFor(() => expect(saveButton()).toBeEnabled());
  expect(canvasNodeIds()).toEqual(nodeIds);
  expect(screen.queryByText("Loading canvas...")).not.toBeInTheDocument();
  expect(screen.getByTestId("react-flow")).toHaveAttribute("data-connectable", "true");
  expect(screen.getByTestId("react-flow")).toHaveAttribute("data-delete-key", "Delete");
}

async function clickSaveAndExpectPut(id: string, nodeIds: string[]) {
  const before = puts.length;
  fireEvent.click(saveButton());
  await waitFor(() => expect(puts.length).toBe(before + 1));
  expect(puts[puts.length - 1]).toEqual({ id, nodeIds });
}

// While B's canvas is not loaded: nothing of A on the canvas, Save, Reserve,
// Save as Template, and Clear disabled, and a click on Save sends nothing.
async function expectNotReadyAndUnsavable() {
  await settle();
  expect(canvasNodeIds()).toEqual([]);
  expect(useTopologyStore.getState().nodes).toEqual([]);
  expect(screen.getByRole("status")).toHaveTextContent("Loading canvas...");
  // No connect and no Delete key on a canvas that is still loading.
  expect(screen.getByTestId("react-flow")).toHaveAttribute("data-connectable", "false");
  expect(screen.getByTestId("react-flow")).toHaveAttribute("data-delete-key", "null");
  expect(saveButton()).toBeDisabled();
  expect(screen.getByRole("button", { name: /Reserve Topology/ })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Save as Template" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Clear canvas" })).toBeDisabled();
  const before = puts.length;
  fireEvent.click(saveButton());
  await settle();
  expect(puts.length).toBe(before);
}

beforeAll(() => {
  HTMLDialogElement.prototype.showModal = vi.fn();
  HTMLDialogElement.prototype.close = vi.fn();
});

// Only the network fixtures reset between tests; the canvas store never does.
beforeEach(() => {
  holds.clear();
  failures.clear();
  forkHold = null;
  puts = [];
  server.use(...handlers());
});

describe("TopologyEditorPage canvas identity (issue #1315)", () => {
  it.each([
    { how: "an in-place id change", viaList: false },
    { how: "the list page (unmount and mount)", viaList: true },
  ])("open A then B uncached, through $how: B's canvas or nothing, never A's", async ({ viaList }) => {
    renderApp("/topology/A");
    await openAndWaitReady("Topology A", ["a-node"]);

    const releaseB = holdHydration("d-b");
    if (viaList) {
      await go("/topology");
      await screen.findByText("topology list page");
    }
    await go("/topology/B");
    await screen.findByText("Topology B");
    await expectNotReadyAndUnsavable();

    releaseB();
    await openAndWaitReady("Topology B", ["b-node"]);
    await clickSaveAndExpectPut("B", ["b-node"]);
  });

  it.each([
    { how: "an in-place id change", viaList: false },
    { how: "the list page (unmount and mount)", viaList: true },
  ])("open A then B cached, through $how: B's name never renders over A's canvas", async ({ viaList }) => {
    renderApp("/topology/B");
    await openAndWaitReady("Topology B", ["b-node"]);
    await go("/topology/A");
    await openAndWaitReady("Topology A", ["a-node"]);

    const releaseB = holdHydration("d-b");
    if (viaList) {
      await go("/topology");
      await screen.findByText("topology list page");
    }
    await go("/topology/B");
    // Cached: B's name is up at once, before its hydration returns.
    expect(screen.getByText("Topology B")).toBeInTheDocument();
    expect(canvasNodeIds()).not.toContain("a-node");
    await expectNotReadyAndUnsavable();

    releaseB();
    await openAndWaitReady("Topology B", ["b-node"]);
    await clickSaveAndExpectPut("B", ["b-node"]);
  });

  it.each([
    { status: 503, message: "Could not load this topology" },
    { status: 404, message: "Topology not found" },
  ])("B's fetch fails ($status): an error state, no canvas, no write action", async ({ status, message }) => {
    renderApp("/topology/A");
    await openAndWaitReady("Topology A", ["a-node"]);

    failures.set("B", status);
    await go("/topology/B");
    expect(await screen.findByRole("alert")).toHaveTextContent(message);
    await settle();
    expect(canvasNodeIds()).toBeNull();
    expect(screen.queryByRole("button", { name: "Save" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Reserve Topology/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Save as Template" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Clear canvas" })).not.toBeInTheDocument();
    expect(useTopologyStore.getState().nodes).toEqual([]);
    expect(puts).toEqual([]);

    fireEvent.click(screen.getByRole("button", { name: "Back to topologies" }));
    expect(await screen.findByText("topology list page")).toBeInTheDocument();
  });

  it("B's hydration resolves after navigating to C: the late B canvas is dropped", async () => {
    renderApp("/topology/A");
    await openAndWaitReady("Topology A", ["a-node"]);

    const releaseB = holdHydration("d-b");
    await go("/topology/B");
    await screen.findByText("Topology B");
    await go("/topology/C");
    await openAndWaitReady("Topology C", ["c-node"]);

    releaseB();
    await settle();
    expect(canvasNodeIds()).toEqual(["c-node"]);
    await clickSaveAndExpectPut("C", ["c-node"]);
  });

  it("B's hydration resolves after navigating to a new, empty topology: the canvas stays empty", async () => {
    renderApp("/topology/A");
    await openAndWaitReady("Topology A", ["a-node"]);

    const releaseB = holdHydration("d-b");
    await go("/topology/B");
    await screen.findByText("Topology B");
    await go("/topology/E");
    await openAndWaitReady("Topology E", []);

    releaseB();
    await settle();
    expect(canvasNodeIds()).toEqual([]);
    await clickSaveAndExpectPut("E", []);
  });

  it("B's hydration resolves after the editor unmounted: it never reaches the next editor", async () => {
    renderApp("/topology/A");
    await openAndWaitReady("Topology A", ["a-node"]);

    const releaseB = holdHydration("d-b");
    await go("/topology/B");
    await screen.findByText("Topology B");
    await go("/topology");
    await screen.findByText("topology list page");
    await go("/topology/E");
    await openAndWaitReady("Topology E", []);

    releaseB();
    await settle();
    expect(canvasNodeIds()).toEqual([]);
    expect(useTopologyStore.getState().nodes).toEqual([]);
    await clickSaveAndExpectPut("E", []);
  });

  it("a new topology with no canvas opened after A: empty canvas, Save PUTs an empty canvas", async () => {
    renderApp("/topology/A");
    await openAndWaitReady("Topology A", ["a-node"]);

    await go("/topology/E");
    await openAndWaitReady("Topology E", []);
    await clickSaveAndExpectPut("E", []);
  });

  it("unmount and remount the same id: not ready until this mount's load lands", async () => {
    renderApp("/topology/A");
    await openAndWaitReady("Topology A", ["a-node"]);

    const releaseA = holdHydration("d-a");
    await go("/topology");
    await screen.findByText("topology list page");
    await go("/topology/A");
    await screen.findByText("Topology A");
    await expectNotReadyAndUnsavable();

    releaseA();
    await openAndWaitReady("Topology A", ["a-node"]);
    await clickSaveAndExpectPut("A", ["a-node"]);
  });

  it("logout clears the canvas; the next user opening a topology never sees the last one's", async () => {
    act(() => useAuthStore.getState().setTokens("access-1", "refresh-1"));
    try {
      renderApp("/topology/A");
      await openAndWaitReady("Topology A", ["a-node"]);

      // Signing out empties the store at once, even before the editor unmounts.
      act(() => useAuthStore.getState().clearAuth());
      expect(useTopologyStore.getState().nodes).toEqual([]);
      await go("/login");
      await screen.findByText("login page");

      // Another user signs in and opens a topology whose hydration is slow.
      act(() => useAuthStore.getState().setTokens("access-2", "refresh-2"));
      const releaseB = holdHydration("d-b");
      await go("/topology/B");
      await screen.findByText("Topology B");
      await expectNotReadyAndUnsavable();
      releaseB();
      await openAndWaitReady("Topology B", ["b-node"]);
      await clickSaveAndExpectPut("B", ["b-node"]);
    } finally {
      act(() => useAuthStore.getState().clearAuth());
    }
  });

  it("entering live edit after A: the canvas is empty while the fork loads, then shows the fork and enables Commit", async () => {
    renderApp("/topology/A");
    await openAndWaitReady("Topology A", ["a-node"]);

    let releaseFork!: () => void;
    forkHold = new Promise<void>((r) => (releaseFork = r));
    await go(`/topology/T?reservationId=${RES_ID}`);
    expect(await screen.findByRole("button", { name: "Loading fork..." })).toBeDisabled();
    await settle();
    expect(canvasNodeIds()).toEqual([]);
    expect(screen.getByRole("button", { name: "Clear canvas" })).toBeDisabled();
    // Plain-mode write actions are not offered in live edit at all.
    expect(screen.queryByRole("button", { name: "Save" })).not.toBeInTheDocument();

    releaseFork();
    expect(await screen.findByRole("button", { name: "Commit to reservation" })).toBeEnabled();
    expect(canvasNodeIds()).toEqual(["fork-node"]);
    expect(screen.getByRole("button", { name: "Clear canvas" })).toBeEnabled();
    expect(puts).toEqual([]);
  });

  it("A, then B pending, then back to A in place: Save stays disabled until A's own load lands", async () => {
    renderApp("/topology/A");
    await openAndWaitReady("Topology A", ["a-node"]);

    const releaseB = holdHydration("d-b");
    await go("/topology/B");
    await screen.findByText("Topology B");
    const releaseA = holdHydration("d-a");
    await go("/topology/A");
    await screen.findByText("Topology A");
    await expectNotReadyAndUnsavable();

    releaseA();
    await openAndWaitReady("Topology A", ["a-node"]);
    releaseB();
    await settle();
    expect(canvasNodeIds()).toEqual(["a-node"]);
    await clickSaveAndExpectPut("A", ["a-node"]);
    expect(puts.filter((p) => p.nodeIds.length === 0)).toEqual([]);
  });

  it("a version preview on A does not survive going to B in place: no exit control, B's canvas, Save PUTs B's nodes", async () => {
    renderApp("/topology/A");
    await openAndWaitReady("Topology A", ["a-node"]);

    fireEvent.click(screen.getByRole("button", { name: "History" }));
    const row = (await screen.findByText("first save of A")).closest("div.px-4") as HTMLElement;
    fireEvent.click(within(row).getByRole("button", { name: "View" }));
    await screen.findByRole("button", { name: /Exit preview \(v1\)/ });

    await go("/topology/B");
    await openAndWaitReady("Topology B", ["b-node"]);
    expect(screen.queryByRole("button", { name: /Exit preview/ })).not.toBeInTheDocument();
    await clickSaveAndExpectPut("B", ["b-node"]);
  });

  it("a version preview of A that resolves after going to B never reaches B's canvas", async () => {
    renderApp("/topology/A");
    await openAndWaitReady("Topology A", ["a-node"]);

    const releaseV1 = holdHydration("d-v1");
    fireEvent.click(screen.getByRole("button", { name: "History" }));
    const row = (await screen.findByText("first save of A")).closest("div.px-4") as HTMLElement;
    fireEvent.click(within(row).getByRole("button", { name: "View" }));
    await settle();

    await go("/topology/B");
    await openAndWaitReady("Topology B", ["b-node"]);
    releaseV1();
    await settle();
    expect(canvasNodeIds()).toEqual(["b-node"]);
    await clickSaveAndExpectPut("B", ["b-node"]);
  });

  it("a fork hydration that resolves after leaving live edit never reaches the next editor", async () => {
    renderApp(`/topology/T?reservationId=${RES_ID}`);
    const releaseFork = holdHydration("d-fork");
    expect(await screen.findByRole("button", { name: "Loading fork..." })).toBeDisabled();

    await go("/topology/E");
    await openAndWaitReady("Topology E", []);
    releaseFork();
    await settle();
    expect(canvasNodeIds()).toEqual([]);
    expect(useTopologyStore.getState().nodes).toEqual([]);
    await clickSaveAndExpectPut("E", []);
  });

  it("the same topology switching between plain mode and live edit loads each source on its own", async () => {
    renderApp("/topology/T");
    await openAndWaitReady("Topology T", ["parent-node"]);

    const releaseFork = holdHydration("d-fork");
    await go(`/topology/T?reservationId=${RES_ID}`);
    expect(await screen.findByRole("button", { name: "Loading fork..." })).toBeDisabled();
    await settle();
    expect(canvasNodeIds()).toEqual([]);
    releaseFork();
    expect(await screen.findByRole("button", { name: "Commit to reservation" })).toBeEnabled();
    expect(canvasNodeIds()).toEqual(["fork-node"]);

    const releaseT = holdHydration("d-parent");
    await go("/topology/T");
    await screen.findByText("Topology T");
    await expectNotReadyAndUnsavable();
    releaseT();
    await openAndWaitReady("Topology T", ["parent-node"]);
    await clickSaveAndExpectPut("T", ["parent-node"]);
  });

  it("a device dropped while the canvas loads is refused, so the load never silently replaces an edit", async () => {
    renderApp("/topology/A");
    await openAndWaitReady("Topology A", ["a-node"]);

    const releaseB = holdHydration("d-b");
    await go("/topology/B");
    await screen.findByText("Topology B");
    const dropped = { id: "d-new", name: "d-new", topology_type: "PHYSICAL", status: "AVAILABLE" };
    fireEvent.drop(screen.getByTestId("react-flow"), {
      dataTransfer: {
        getData: (type: string) => (type === "application/herd-device" ? JSON.stringify(dropped) : ""),
      },
    });
    await expectNotReadyAndUnsavable();

    releaseB();
    await openAndWaitReady("Topology B", ["b-node"]);
    await clickSaveAndExpectPut("B", ["b-node"]);
  });

  it("under StrictMode's double mount the editor still loads A and saves A's canvas", async () => {
    renderApp("/topology/B", true);
    await openAndWaitReady("Topology B", ["b-node"]);
    await go("/topology/A");
    await openAndWaitReady("Topology A", ["a-node"]);
    await clickSaveAndExpectPut("A", ["a-node"]);
  });
});
