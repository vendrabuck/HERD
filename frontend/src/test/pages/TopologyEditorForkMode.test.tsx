import { http, HttpResponse } from "msw";
import { render, screen, waitFor, fireEvent, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

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

// React Flow renders a heavy canvas that does not work in jsdom. Stub the visual
// components but keep the provider/hooks and the store's graph helpers real.
vi.mock("@xyflow/react", async () => {
  const actual = await vi.importActual<typeof import("@xyflow/react")>("@xyflow/react");
  return {
    ...actual,
    ReactFlow: ({ children }: { children?: React.ReactNode }) => (
      <div data-testid="react-flow">{children}</div>
    ),
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

vi.mock("@/components/topology-editor/nodes/DeviceNode", () => ({ DeviceNode: () => null }));
vi.mock("@/components/topology-editor/edges/LayerEdge", () => ({ LayerEdge: () => null }));

import { server } from "../mocks/server";
import { TopologyEditorPage } from "@/pages/TopologyEditorPage";
import { useTopologyStore } from "@/stores/topologyStore";

const TOPO_ID = "topo-1";
const RES_ID = "res-1";

function forkNode(id: string, deviceId: string) {
  return {
    id,
    type: "deviceNode",
    position: { x: 0, y: 0 },
    data: { device: { id: deviceId, name: deviceId, topology_type: "PHYSICAL" }, label: deviceId, topologyType: "PHYSICAL" },
  };
}

function makeFork(overrides: Record<string, unknown> = {}) {
  return {
    id: "fork-1",
    reservation_id: RES_ID,
    parent_topology_id: TOPO_ID,
    parent_version_id: null,
    status: "ACTIVE",
    canvas_data: { nodes: [forkNode("fork-node", "d-fork")], edges: [], selectedEdgeLayer: "L2" },
    created_at: "2026-06-01T00:00:00Z",
    updated_at: "2026-06-01T00:00:00Z",
    connections: [],
    versions: [
      { id: "fv-1", fork_id: "fork-1", version_number: 1, restored_from_id: null, created_at: "2026-06-01T00:00:00Z" },
    ],
    // null except while the draft holds a restored-but-unsaved snapshot
    // (issue #622 contract, revised 2026-08-28).
    draft_restored_from_id: null,
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
  canvas_data: { nodes: [forkNode("parent-node", "d-parent")], edges: [], selectedEdgeLayer: "L2" },
};

const RESERVATION = {
  id: RES_ID,
  user_id: "u",
  owner_name: "u",
  device_ids: ["d-fork"],
  topology_id: TOPO_ID,
  topology_type: "PHYSICAL",
  purpose: "fork test",
  start_time: "2026-06-01T00:00:00Z",
  end_time: "2026-06-02T00:00:00Z",
  status: "ACTIVE",
  created_at: "2026-05-01T00:00:00Z",
};

function baseHandlers(fork: Record<string, unknown>, reservation: Record<string, unknown> = RESERVATION) {
  return [
    http.get(`/api/reservations/${RES_ID}/fork`, () => HttpResponse.json(fork)),
    http.get(`/api/cabling/topologies/${TOPO_ID}`, () => HttpResponse.json(PARENT_TOPOLOGY)),
    http.get("/api/reservations/", () =>
      HttpResponse.json({ items: [reservation], total: 1, skip: 0, limit: 500 }),
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
      <MemoryRouter initialEntries={[`/topology/${TOPO_ID}?reservationId=${RES_ID}`]}>
        <Routes>
          <Route path="/topology/:id" element={<TopologyEditorPage />} />
          <Route path="/reservations" element={<div>reservations page</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
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

describe("TopologyEditorPage live-edit fork mode", () => {
  it("loads the reservation fork canvas, not the parent topology canvas", async () => {
    server.use(...baseHandlers(makeFork()));
    renderPage();

    await waitFor(() =>
      expect(useTopologyStore.getState().nodes.map((n) => n.id)).toContain("fork-node"),
    );
    // The parent topology's node must never reach the canvas in live-edit mode.
    expect(useTopologyStore.getState().nodes.map((n) => n.id)).not.toContain("parent-node");
    // The live-edit bar is shown for an editable (ACTIVE) fork.
    expect(screen.getByText("EDITING LIVE RESERVATION")).toBeInTheDocument();
  });

  it("commit calls the fork save and the device PATCH, never the parent topology PUT", async () => {
    let forkSaveHit = false;
    let parentPutHit = false;
    let devicePatchHit = false;

    server.use(
      ...baseHandlers(makeFork()),
      http.post(`/api/reservations/${RES_ID}/fork/save`, () => {
        forkSaveHit = true;
        return HttpResponse.json({
          fork_id: "fork-1",
          version_number: 2,
          released: [],
          built: [{ device_a_id: "d-fork", port_a: "1", device_b_id: "d-x", port_b: "2", layer: "L2" }],
          unchanged_count: 0,
        });
      }),
      http.put(`/api/cabling/topologies/${TOPO_ID}`, () => {
        parentPutHit = true;
        return HttpResponse.json(PARENT_TOPOLOGY);
      }),
      http.patch(`/api/reservations/${RES_ID}`, () => {
        devicePatchHit = true;
        return HttpResponse.json(RESERVATION);
      }),
    );

    renderPage();
    await waitFor(() =>
      expect(useTopologyStore.getState().nodes.map((n) => n.id)).toContain("fork-node"),
    );

    fireEvent.click(screen.getByRole("button", { name: "Commit to reservation" }));

    await waitFor(() => expect(forkSaveHit).toBe(true));
    await waitFor(() => expect(devicePatchHit).toBe(true));
    // The core promise of the epic: the parent topology PUT is gone from this path.
    expect(parentPutHit).toBe(false);
    // The save result is surfaced as a toast.
    await waitFor(() => expect(toastCustom).toHaveBeenCalled());
  });

  it("PATCH-adds a newly drawn device to the reservation BEFORE saving the fork (issue #701)", async () => {
    // d-new is on the canvas but not yet in the reservation's device_ids: the
    // membership check on save would 409 unless the device joins the
    // reservation first.
    const calls: string[] = [];
    const patchBodies: unknown[] = [];
    let patchCount = 0;

    server.use(
      ...baseHandlers(
        makeFork({
          canvas_data: {
            nodes: [forkNode("fork-node", "d-fork"), forkNode("new-node", "d-new")],
            edges: [],
            selectedEdgeLayer: "L2",
          },
        }),
      ),
      http.patch(`/api/reservations/${RES_ID}`, async ({ request }) => {
        calls.push("patch");
        patchCount += 1;
        patchBodies.push(await request.json());
        return HttpResponse.json(RESERVATION);
      }),
      http.post(`/api/reservations/${RES_ID}/fork/save`, () => {
        calls.push("save");
        return HttpResponse.json({
          fork_id: "fork-1",
          version_number: 2,
          released: [],
          built: [],
          unchanged_count: 0,
        });
      }),
    );

    renderPage();
    await waitFor(() =>
      expect(useTopologyStore.getState().nodes.map((n) => n.id)).toContain("new-node"),
    );

    fireEvent.click(screen.getByRole("button", { name: "Commit to reservation" }));

    await waitFor(() => expect(calls).toEqual(["patch", "save"]));
    const body = patchBodies[0] as { device_ids: string[] };
    expect(new Set(body.device_ids)).toEqual(new Set(["d-fork", "d-new"]));

    // Nothing was removed, so the pre-save add already left the reservation's
    // device set at exactly the canvas's: no redundant settle-PATCH after save.
    await waitFor(() => expect(toastCustom).toHaveBeenCalled());
    expect(patchCount).toBe(1);
  });

  it("a failed device-set PATCH blocks the fork save entirely (issue #701)", async () => {
    let forkSaveHit = false;

    server.use(
      ...baseHandlers(
        makeFork({
          canvas_data: {
            nodes: [forkNode("fork-node", "d-fork"), forkNode("new-node", "d-new")],
            edges: [],
            selectedEdgeLayer: "L2",
          },
        }),
      ),
      http.patch(`/api/reservations/${RES_ID}`, () =>
        HttpResponse.json({ detail: "Cannot add device to a full reservation" }, { status: 409 }),
      ),
      http.post(`/api/reservations/${RES_ID}/fork/save`, () => {
        forkSaveHit = true;
        return HttpResponse.json({});
      }),
    );

    renderPage();
    await waitFor(() =>
      expect(useTopologyStore.getState().nodes.map((n) => n.id)).toContain("new-node"),
    );

    fireEvent.click(screen.getByRole("button", { name: "Commit to reservation" }));

    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith("Cannot add device to a full reservation"),
    );
    // The fork save must never be attempted once the device-set PATCH failed:
    // saving now would still 409 on membership.
    expect(forkSaveHit).toBe(false);
  });

  it("PATCH-removes a device from the reservation only AFTER the fork save succeeds (issue #701)", async () => {
    // The reservation still holds d-other, but it has been removed from the
    // canvas: the prune semantics require the settle-PATCH to run after the
    // save, never before, since it prunes wiring off the SAVED intended set.
    const calls: string[] = [];
    const patchBodies: unknown[] = [];
    let patchCount = 0;
    const reservationWithExtraDevice = { ...RESERVATION, device_ids: ["d-fork", "d-other"] };

    server.use(
      ...baseHandlers(makeFork(), reservationWithExtraDevice),
      http.post(`/api/reservations/${RES_ID}/fork/save`, () => {
        calls.push("save");
        return HttpResponse.json({
          fork_id: "fork-1",
          version_number: 2,
          released: [],
          built: [],
          unchanged_count: 0,
        });
      }),
      http.patch(`/api/reservations/${RES_ID}`, async ({ request }) => {
        calls.push("patch");
        patchCount += 1;
        patchBodies.push(await request.json());
        return HttpResponse.json(reservationWithExtraDevice);
      }),
    );

    renderPage();
    await waitFor(() =>
      expect(useTopologyStore.getState().nodes.map((n) => n.id)).toContain("fork-node"),
    );

    fireEvent.click(screen.getByRole("button", { name: "Commit to reservation" }));

    await waitFor(() => expect(calls).toEqual(["save", "patch"]));
    const body = patchBodies[0] as { device_ids: string[] };
    expect(body.device_ids).toEqual(["d-fork"]);
    expect(patchCount).toBe(1);
  });

  it("renders read-only when the fork is archived (ended reservation as-built)", async () => {
    server.use(...baseHandlers(makeFork({ status: "ARCHIVED" })));
    renderPage();

    await waitFor(() =>
      expect(useTopologyStore.getState().nodes.map((n) => n.id)).toContain("fork-node"),
    );

    // The read-only as-built bar and toolbar badge are shown, and the editable
    // commit affordance is absent.
    expect(screen.getByText("AS-BUILT RECORD (READ-ONLY)")).toBeInTheDocument();
    expect(screen.getByText(/As-built \(read-only\)/)).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Commit to reservation" }),
    ).not.toBeInTheDocument();
  });

  it("previewing a fork version shows the history banner and locks editing (issue #622)", async () => {
    const fork = makeFork();
    server.use(
      ...baseHandlers(fork),
      http.get(`/api/reservations/${RES_ID}/fork/versions/fv-1`, () =>
        HttpResponse.json({
          id: "fv-1",
          fork_id: "fork-1",
          version_number: 1,
          restored_from_id: null,
          created_at: "2026-06-01T00:00:00Z",
          canvas_data: { nodes: [forkNode("v1-node", "d-v1")], edges: [], selectedEdgeLayer: "L2" },
        }),
      ),
    );
    renderPage();

    await waitFor(() =>
      expect(useTopologyStore.getState().nodes.map((n) => n.id)).toContain("fork-node"),
    );

    // Editable before entering preview.
    expect(screen.getByText("EDITING LIVE RESERVATION")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "History" }));
    fireEvent.click(screen.getByRole("button", { name: "Preview" }));

    await waitFor(() =>
      expect(useTopologyStore.getState().nodes.map((n) => n.id)).toContain("v1-node"),
    );
    // Read-only history banner is up, and the live-edit commit affordance
    // (and thus Save/commit) is gone while it is.
    expect(screen.getByText(/Previewing version 1/)).toBeInTheDocument();
    expect(screen.queryByText("EDITING LIVE RESERVATION")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Commit to reservation" }),
    ).not.toBeInTheDocument();

    // Exiting restores the live draft the user was editing.
    fireEvent.click(screen.getByRole("button", { name: /Exit preview/ }));
    await waitFor(() =>
      expect(useTopologyStore.getState().nodes.map((n) => n.id)).toContain("fork-node"),
    );
    expect(screen.getByText("EDITING LIVE RESERVATION")).toBeInTheDocument();
  });

  it("closing the history panel while previewing also exits the preview (issue #622 review)", async () => {
    const fork = makeFork();
    server.use(
      ...baseHandlers(fork),
      http.get(`/api/reservations/${RES_ID}/fork/versions/fv-1`, () =>
        HttpResponse.json({
          id: "fv-1",
          fork_id: "fork-1",
          version_number: 1,
          restored_from_id: null,
          created_at: "2026-06-01T00:00:00Z",
          canvas_data: { nodes: [forkNode("v1-node", "d-v1")], edges: [], selectedEdgeLayer: "L2" },
        }),
      ),
    );
    renderPage();

    await waitFor(() =>
      expect(useTopologyStore.getState().nodes.map((n) => n.id)).toContain("fork-node"),
    );

    fireEvent.click(screen.getByRole("button", { name: "History" }));
    fireEvent.click(screen.getByRole("button", { name: "Preview" }));

    await waitFor(() =>
      expect(useTopologyStore.getState().nodes.map((n) => n.id)).toContain("v1-node"),
    );
    expect(screen.getByText(/Previewing version 1/)).toBeInTheDocument();

    // Close the panel via its own close control, NOT the banner's Exit
    // button: this must also exit the preview, not just hide the panel.
    fireEvent.click(screen.getByRole("button", { name: "Close history panel" }));

    await waitFor(() => expect(screen.queryByText(/Previewing version 1/)).not.toBeInTheDocument());
    expect(screen.getByText("EDITING LIVE RESERVATION")).toBeInTheDocument();
    await waitFor(() =>
      expect(useTopologyStore.getState().nodes.map((n) => n.id)).toContain("fork-node"),
    );
  });

  it("a diff overlay's removed-edge annotation never fires a pathfind request for that pair (issue #622 review)", async () => {
    // The current draft has two devices and NO edge between them; v1 (the
    // diff base) had an edge between the same two devices. Diffing v1 to
    // current therefore overlays a diffStatus:"removed" annotation edge for
    // that pair onto the canvas: a pair with no real committed wire.
    const secondNode = forkNode("other-node", "d-other");
    const fork = makeFork({
      canvas_data: {
        nodes: [forkNode("fork-node", "d-fork"), secondNode],
        edges: [],
        selectedEdgeLayer: "L2",
      },
    });
    const pathfindBodies: unknown[] = [];
    server.use(
      ...baseHandlers(fork),
      http.get(`/api/reservations/${RES_ID}/fork/versions/fv-1`, () =>
        HttpResponse.json({
          id: "fv-1",
          fork_id: "fork-1",
          version_number: 1,
          restored_from_id: null,
          created_at: "2026-06-01T00:00:00Z",
          canvas_data: {
            nodes: [forkNode("fork-node", "d-fork"), secondNode],
            edges: [
              {
                id: "e-v1",
                source: "fork-node",
                target: "other-node",
                type: "layerEdge",
                data: { layer: "L1", source_port_name: "eth1", target_port_name: "eth1" },
              },
            ],
            selectedEdgeLayer: "L2",
          },
        }),
      ),
      http.post("/api/cabling/pathfind/batch", async ({ request }) => {
        const body = await request.json();
        pathfindBodies.push(body);
        return HttpResponse.json({ results: [] });
      }),
    );
    renderPage();

    await waitFor(() =>
      expect(useTopologyStore.getState().nodes.map((n) => n.id)).toContain("fork-node"),
    );

    fireEvent.click(screen.getByRole("button", { name: "History" }));
    fireEvent.click(screen.getByRole("button", { name: "Diff" }));
    fireEvent.click(screen.getByRole("button", { name: "Compare" }));

    await waitFor(() => expect(screen.getByText(/Diff: v1/)).toBeInTheDocument());
    await waitFor(() =>
      expect(useTopologyStore.getState().edges.some((e) => e.data?.diffStatus === "removed")).toBe(
        true,
      ),
    );

    // No pathfind request, across the whole diff-mode session, ever asked
    // about the d-fork/d-other pair: the diff overlay's removed-edge
    // annotation must never be treated as a real wire needing validation.
    const askedAboutPair = pathfindBodies.some((body) =>
      (body as { pairs: { source_device_id: string; target_device_id: string }[] }).pairs.some(
        (p) =>
          (p.source_device_id === "d-fork" && p.target_device_id === "d-other") ||
          (p.source_device_id === "d-other" && p.target_device_id === "d-fork"),
      ),
    );
    expect(askedAboutPair).toBe(false);
  });
});

describe("TopologyEditorPage handleCommitToReservation error branches", () => {
  it("disables the Commit button with the invalid-edge count when an edge has no physical path, and never calls the fork save", async () => {
    // LiveEditBar itself disables the Commit button under the identical
    // invalidEdgeCount > 0 condition handleCommitToReservation guards
    // against, so this scenario is unreachable via a real click; the
    // page-level check is defense in depth. This test pins the reachable,
    // user-visible half of that guard: the button is disabled and titled
    // with the exact count, and the fork-save endpoint is never hit.
    let forkSaveHit = false;
    const fork = makeFork({
      canvas_data: {
        nodes: [forkNode("fork-node", "d-fork"), forkNode("other-node", "d-other")],
        edges: [
          {
            id: "e-bad",
            source: "fork-node",
            target: "other-node",
            type: "layerEdge",
            data: { layer: "L1", pathValid: false },
          },
        ],
        selectedEdgeLayer: "L2",
      },
    });
    server.use(
      ...baseHandlers(fork),
      http.post(`/api/reservations/${RES_ID}/fork/save`, () => {
        forkSaveHit = true;
        return HttpResponse.json({});
      }),
    );
    renderPage();
    await waitFor(() =>
      expect(useTopologyStore.getState().nodes.map((n) => n.id)).toContain("fork-node"),
    );

    const commitButton = await screen.findByRole("button", { name: "Commit to reservation" });
    expect(commitButton).toBeDisabled();
    expect(commitButton).toHaveAttribute("title", "Cannot commit: 1 edge have no physical path");

    fireEvent.click(commitButton);
    expect(forkSaveHit).toBe(false);
  });

  it("a structured 409 port-claim conflict opens the conflict dialog and keeps the drawing", async () => {
    server.use(
      ...baseHandlers(makeFork()),
      http.post(`/api/reservations/${RES_ID}/fork/save`, () =>
        HttpResponse.json(
          {
            detail: {
              message: "Ports already claimed by another reservation",
              conflicts: [{ reservation_id: "res-x", device_id: "d-x", port: "eth1" }],
            },
          },
          { status: 409 },
        ),
      ),
    );
    renderPage();
    await waitFor(() =>
      expect(useTopologyStore.getState().nodes.map((n) => n.id)).toContain("fork-node"),
    );

    fireEvent.click(screen.getByRole("button", { name: "Commit to reservation" }));

    const conflictHeading = await screen.findByText("Ports already claimed by another reservation");
    expect(screen.getByText(/port eth1/)).toBeInTheDocument();
    // The canvas is untouched: the fork node is still there for the user to rework.
    expect(useTopologyStore.getState().nodes.map((n) => n.id)).toContain("fork-node");

    // Its own close button clears the conflict state.
    const conflictDialog = conflictHeading.closest("dialog") as HTMLDialogElement;
    fireEvent.click(
      within(conflictDialog).getByRole("button", { name: "Back to editing", hidden: true }),
    );
    await waitFor(() => expect(conflictDialog.open).toBe(false));
  });

  it("a plain 409 with a string detail toasts that string verbatim", async () => {
    server.use(
      ...baseHandlers(makeFork()),
      http.post(`/api/reservations/${RES_ID}/fork/save`, () =>
        HttpResponse.json({ detail: "Fork is not ACTIVE" }, { status: 409 }),
      ),
    );
    renderPage();
    await waitFor(() =>
      expect(useTopologyStore.getState().nodes.map((n) => n.id)).toContain("fork-node"),
    );

    fireEvent.click(screen.getByRole("button", { name: "Commit to reservation" }));

    await waitFor(() => expect(toastError).toHaveBeenCalledWith("Fork is not ACTIVE"));
  });

  it("a fork_device_not_member 409 names the offending devices in plain words (issue #701)", async () => {
    // The canvas only names d-fork, already a reservation member, so this
    // pins the save-time refusal (a device the PATCH could not add, or a
    // race with a concurrent removal), not the pre-save PATCH path.
    server.use(
      ...baseHandlers(makeFork()),
      http.post(`/api/reservations/${RES_ID}/fork/save`, () =>
        HttpResponse.json(
          { detail: { error: "fork_device_not_member", device_ids: ["d-x", "d-y"] } },
          { status: 409 },
        ),
      ),
    );
    renderPage();
    await waitFor(() =>
      expect(useTopologyStore.getState().nodes.map((n) => n.id)).toContain("fork-node"),
    );

    fireEvent.click(screen.getByRole("button", { name: "Commit to reservation" }));

    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith(
        "These devices are not part of the reservation: d-x, d-y",
      ),
    );
  });

  it("a transport failure with no detail falls back to the default save-failed message", async () => {
    server.use(
      ...baseHandlers(makeFork()),
      http.post(`/api/reservations/${RES_ID}/fork/save`, () =>
        HttpResponse.json({}, { status: 500 }),
      ),
    );
    renderPage();
    await waitFor(() =>
      expect(useTopologyStore.getState().nodes.map((n) => n.id)).toContain("fork-node"),
    );

    fireEvent.click(screen.getByRole("button", { name: "Commit to reservation" }));

    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith("Failed to save the reservation fork"),
    );
  });

  it("a device-set PATCH failure after a successful fork save gets its own toast, distinct from a save failure", async () => {
    server.use(
      ...baseHandlers(makeFork()),
      http.post(`/api/reservations/${RES_ID}/fork/save`, () =>
        HttpResponse.json({
          fork_id: "fork-1",
          version_number: 2,
          released: [],
          built: [],
          unchanged_count: 0,
        }),
      ),
      http.patch(`/api/reservations/${RES_ID}`, () => HttpResponse.json({ detail: "boom" }, { status: 500 })),
    );
    renderPage();
    await waitFor(() =>
      expect(useTopologyStore.getState().nodes.map((n) => n.id)).toContain("fork-node"),
    );

    fireEvent.click(screen.getByRole("button", { name: "Commit to reservation" }));

    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith(
        "Fork saved, but updating the reservation's device set failed; commit again to retry",
      ),
    );
    // The fork-save toast still fired: the two failures are reported distinctly.
    expect(toastCustom).toHaveBeenCalled();
  });
});
