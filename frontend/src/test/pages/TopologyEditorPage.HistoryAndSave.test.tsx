import { http, HttpResponse } from "msw";
import { render, screen, waitFor, fireEvent, act, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import type { Node } from "@xyflow/react";

// Covers TopologyEditorPage.tsx surfaces not exercised by the other four
// TopologyEditor*.test.tsx files: save-as-template, version history
// preview/restore/diff/close, the pathfind path-status reconcile effect,
// isValidConnection's missing-node and topology-type-mismatch branches, the
// reserve modal's initialDynamicEntries prefill-on-mount behavior, and a
// handful of toolbar handlers (edge layer select, quick-connect toggle,
// description input, back navigation, MiniMap nodeColor).

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
    MiniMap: (props: { nodeColor?: (node: { type?: string; data?: unknown }) => string }) => {
      // Exercise the MiniMap nodeColor callback directly, the way React Flow
      // itself would call it once per rendered node, for each node kind the
      // page's own nodeColor implementation branches on.
      props.nodeColor?.({ type: "dynamicPlaceholderNode" });
      props.nodeColor?.({ type: "networkElementNode" });
      props.nodeColor?.({ type: "deviceNode", data: { device: { topology_type: "CLOUD" } } });
      props.nodeColor?.({ type: "deviceNode", data: { device: { topology_type: "PHYSICAL" } } });
      return <div data-testid="rf-minimap" />;
    },
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

const TOPO_ID = "topo-hist-1";

function deviceNode(
  id: string,
  deviceId: string,
  topologyType: "PHYSICAL" | "CLOUD" = "PHYSICAL",
): Node<CanvasNodeData> {
  return {
    id,
    type: "deviceNode",
    position: { x: 0, y: 0 },
    data: {
      device: { id: deviceId, name: deviceId, topology_type: topologyType, status: "AVAILABLE" },
      label: deviceId,
      topologyType,
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
      HttpResponse.json({
        items: [{ id: "dt-1", name: "Ubuntu VM", template_type: "dynamic", icon: null }],
        total: 1,
        skip: 0,
        limit: 500,
      }),
    ),
    http.get(`/api/cabling/topologies/${TOPO_ID}/versions`, () =>
      HttpResponse.json({ items: [VERSION_2, VERSION_1], total: 2, skip: 0, limit: 200 }),
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

// The Save-as-Template modal's submit button also reads "Save", same as the
// toolbar's own Save button, so every query for it is scoped to the modal's
// own form via the Template name input's ancestor.
function templateForm(): HTMLElement {
  return screen.getByLabelText("Template name").closest("form") as HTMLElement;
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

describe("TopologyEditorPage save as template", () => {
  it("submitting creates a template, closes the modal, and clears the name field", async () => {
    let postBody: Record<string, unknown> | null = null;
    server.use(
      ...baseHandlers(),
      http.post(`/api/cabling/templates/from-topology/${TOPO_ID}`, async ({ request }) => {
        postBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({ id: "tpl-1", name: "My template" });
      }),
    );
    await renderPageWithCanvas([deviceNode("n-dev", "d-1")]);

    fireEvent.click(screen.getByRole("button", { name: "Save as Template" }));
    const nameInput = await screen.findByLabelText("Template name");
    fireEvent.change(nameInput, { target: { value: "My template" } });
    fireEvent.click(within(templateForm()).getByRole("button", { name: "Save" }));

    await waitFor(() => expect(postBody).toEqual({ name: "My template" }));
    // The dialog element stays mounted (Modal calls the native .close()
    // rather than unmounting), so assert on its own open state, not on
    // whether the input is still findable in the DOM.
    await waitFor(() =>
      expect(screen.getByLabelText("Template name").closest("dialog")).not.toHaveAttribute("open"),
    );
  });

  it("a failed submit shows the server's detail message and keeps the modal open", async () => {
    server.use(
      ...baseHandlers(),
      http.post(`/api/cabling/templates/from-topology/${TOPO_ID}`, () =>
        HttpResponse.json({ detail: "Name already in use" }, { status: 409 }),
      ),
    );
    await renderPageWithCanvas([deviceNode("n-dev", "d-1")]);

    fireEvent.click(screen.getByRole("button", { name: "Save as Template" }));
    const nameInput = await screen.findByLabelText("Template name");
    fireEvent.change(nameInput, { target: { value: "Dup" } });
    fireEvent.click(within(templateForm()).getByRole("button", { name: "Save" }));

    expect(await screen.findByText("Name already in use")).toBeInTheDocument();
    // The modal stayed open with the entered name intact.
    expect(screen.getByLabelText("Template name")).toHaveValue("Dup");
  });

  it("Cancel closes the modal and clears the name and error", async () => {
    server.use(...baseHandlers());
    await renderPageWithCanvas([deviceNode("n-dev", "d-1")]);

    fireEvent.click(screen.getByRole("button", { name: "Save as Template" }));
    const nameInput = await screen.findByLabelText("Template name");
    fireEvent.change(nameInput, { target: { value: "Draft name" } });
    fireEvent.click(within(templateForm()).getByRole("button", { name: "Cancel" }));

    await waitFor(() =>
      expect(screen.getByLabelText("Template name").closest("dialog")).not.toHaveAttribute("open"),
    );
    // Reopening shows a fresh blank field, proving state was cleared on cancel.
    fireEvent.click(screen.getByRole("button", { name: "Save as Template" }));
    expect(await screen.findByLabelText("Template name")).toHaveValue("");
  });
});

describe("TopologyEditorPage version history", () => {
  it("Preview loads a version's canvas as ghost nodes and Exit preview restores the live draft", async () => {
    server.use(
      ...baseHandlers(),
      http.get(`/api/cabling/topologies/${TOPO_ID}/versions/v-1`, () =>
        HttpResponse.json({
          ...VERSION_1,
          canvas_data: {
            nodes: [deviceNode("v1-node", "d-v1")],
            edges: [],
            selectedEdgeLayer: "L2",
          },
        }),
      ),
    );
    await renderPageWithCanvas([deviceNode("n-live", "d-live")]);

    fireEvent.click(screen.getByRole("button", { name: "History" }));
    await screen.findByText("v1");
    const v1Row = screen.getByText("v1").closest("div.px-4") as HTMLElement;
    fireEvent.click(within(v1Row).getByRole("button", { name: "View" }));

    await waitFor(() =>
      expect(useTopologyStore.getState().nodes.map((n) => n.id)).toContain("v1-node"),
    );
    expect(
      useTopologyStore.getState().nodes.every((n) => (n.data as { isProposal?: boolean }).isProposal),
    ).toBe(true);
    expect(screen.getByRole("button", { name: /Exit preview \(v1\)/ })).toBeInTheDocument();
    // The description input is disabled while previewing.
    expect(screen.getByLabelText("Version description")).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: /Exit preview/ }));

    await waitFor(() =>
      expect(useTopologyStore.getState().nodes.map((n) => n.id)).toContain("n-live"),
    );
    expect(screen.queryByRole("button", { name: /Exit preview/ })).not.toBeInTheDocument();
  });

  it("Preview shows an error toast when the version has no canvas data", async () => {
    server.use(
      ...baseHandlers(),
      http.get(`/api/cabling/topologies/${TOPO_ID}/versions/v-1`, () =>
        HttpResponse.json({ ...VERSION_1, canvas_data: null }),
      ),
    );
    await renderPageWithCanvas([deviceNode("n-live", "d-live")]);

    fireEvent.click(screen.getByRole("button", { name: "History" }));
    await screen.findByText("v1");
    const v1Row = screen.getByText("v1").closest("div.px-4") as HTMLElement;
    fireEvent.click(within(v1Row).getByRole("button", { name: "View" }));

    await waitFor(() => expect(toastError).toHaveBeenCalledWith("Version has no canvas data"));
    // No preview state was entered.
    expect(screen.queryByRole("button", { name: /Exit preview/ })).not.toBeInTheDocument();
  });

  it("Preview shows an error toast when the version fetch fails", async () => {
    server.use(
      ...baseHandlers(),
      http.get(`/api/cabling/topologies/${TOPO_ID}/versions/v-1`, () =>
        HttpResponse.json({ detail: "boom" }, { status: 500 }),
      ),
    );
    await renderPageWithCanvas([deviceNode("n-live", "d-live")]);

    fireEvent.click(screen.getByRole("button", { name: "History" }));
    await screen.findByText("v1");
    const v1Row = screen.getByText("v1").closest("div.px-4") as HTMLElement;
    fireEvent.click(within(v1Row).getByRole("button", { name: "View" }));

    await waitFor(() => expect(toastError).toHaveBeenCalledWith("Failed to load version"));
  });

  it("Compare opens the diff dialog for two selected versions, and Close clears it", async () => {
    server.use(
      ...baseHandlers(),
      http.get(`/api/cabling/topologies/${TOPO_ID}/versions/diff`, () =>
        HttpResponse.json({
          version_a: "v-1",
          version_b: "v-2",
          nodes_added: [],
          nodes_removed: [],
          nodes_modified: [],
          edges_added: [],
          edges_removed: [],
          edges_modified: [],
        }),
      ),
    );
    await renderPageWithCanvas([deviceNode("n-live", "d-live")]);

    fireEvent.click(screen.getByRole("button", { name: "History" }));
    await screen.findByText("v1");
    fireEvent.click(screen.getByLabelText("Select version 1"));
    fireEvent.click(screen.getByLabelText("Select version 2"));
    fireEvent.click(screen.getByRole("button", { name: "Compare" }));

    const diffHeading = await screen.findByRole("heading", { name: "Diff v1 to v2" });
    expect(diffHeading).toBeInTheDocument();

    const diffDialog = diffHeading.closest("dialog") as HTMLElement;
    fireEvent.click(within(diffDialog).getByRole("button", { name: "Close dialog" }));
    await waitFor(() =>
      expect(screen.queryByRole("heading", { name: "Diff v1 to v2" })).not.toBeInTheDocument(),
    );
  });

  it("Restore succeeds: loads the returned canvas, shows a success toast, and clears the restore target", async () => {
    server.use(
      ...baseHandlers(),
      http.post(`/api/cabling/topologies/${TOPO_ID}/versions/v-1/restore`, () =>
        HttpResponse.json({
          ...PARENT_TOPOLOGY,
          canvas_data: {
            nodes: [deviceNode("restored-node", "d-restored")],
            edges: [],
            selectedEdgeLayer: "L2",
          },
        }),
      ),
    );
    await renderPageWithCanvas([deviceNode("n-live", "d-live")]);

    fireEvent.click(screen.getByRole("button", { name: "History" }));
    await screen.findByText("v1");
    const v1Row = screen.getByText("v1").closest("div.px-4") as HTMLElement;
    fireEvent.click(within(v1Row).getByRole("button", { name: "Restore" }));

    const restoreTitle = await screen.findByText("Restore version v1");
    const restoreDialog = restoreTitle.closest("dialog") as HTMLElement;
    fireEvent.click(within(restoreDialog).getByRole("button", { name: "Restore" }));

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith("Restored v1"));
    await waitFor(() =>
      expect(useTopologyStore.getState().nodes.map((n) => n.id)).toContain("restored-node"),
    );
    expect(screen.queryByText("Restore version v1")).not.toBeInTheDocument();
  });

  it("Restore blocked by active reservations (409) surfaces the blocking list instead of restoring", async () => {
    server.use(
      ...baseHandlers(),
      http.post(`/api/cabling/topologies/${TOPO_ID}/versions/v-1/restore`, () =>
        HttpResponse.json(
          { detail: { reservations: [{ id: "res-x", status: "ACTIVE" }] } },
          { status: 409 },
        ),
      ),
    );
    await renderPageWithCanvas([deviceNode("n-live", "d-live")]);

    fireEvent.click(screen.getByRole("button", { name: "History" }));
    await screen.findByText("v1");
    const v1Row = screen.getByText("v1").closest("div.px-4") as HTMLElement;
    fireEvent.click(within(v1Row).getByRole("button", { name: "Restore" }));
    const restoreTitle1 = await screen.findByText("Restore version v1");
    const restoreDialog1 = restoreTitle1.closest("dialog") as HTMLElement;
    fireEvent.click(within(restoreDialog1).getByRole("button", { name: "Restore" }));

    expect(await screen.findByText(/Restore blocked by active reservations/)).toBeInTheDocument();
    expect(screen.getByText(/res-x \(ACTIVE\)/)).toBeInTheDocument();
    // The dialog is still open; the canvas was not touched.
    expect(useTopologyStore.getState().nodes.map((n) => n.id)).toEqual(["n-live"]);
  });

  it("a non-409 restore failure shows a generic error toast", async () => {
    server.use(
      ...baseHandlers(),
      http.post(`/api/cabling/topologies/${TOPO_ID}/versions/v-1/restore`, () =>
        HttpResponse.json({ detail: "boom" }, { status: 500 }),
      ),
    );
    await renderPageWithCanvas([deviceNode("n-live", "d-live")]);

    fireEvent.click(screen.getByRole("button", { name: "History" }));
    await screen.findByText("v1");
    const v1Row = screen.getByText("v1").closest("div.px-4") as HTMLElement;
    fireEvent.click(within(v1Row).getByRole("button", { name: "Restore" }));
    const restoreTitle2 = await screen.findByText("Restore version v1");
    const restoreDialog2 = restoreTitle2.closest("dialog") as HTMLElement;
    fireEvent.click(within(restoreDialog2).getByRole("button", { name: "Restore" }));

    await waitFor(() => expect(toastError).toHaveBeenCalledWith("Restore failed"));
  });

  it("closing the restore dialog clears the blocking-reservations list", async () => {
    server.use(
      ...baseHandlers(),
      http.post(`/api/cabling/topologies/${TOPO_ID}/versions/v-1/restore`, () =>
        HttpResponse.json(
          { detail: { reservations: [{ id: "res-x", status: "ACTIVE" }] } },
          { status: 409 },
        ),
      ),
    );
    await renderPageWithCanvas([deviceNode("n-live", "d-live")]);

    fireEvent.click(screen.getByRole("button", { name: "History" }));
    await screen.findByText("v1");
    const v1Row = screen.getByText("v1").closest("div.px-4") as HTMLElement;
    fireEvent.click(within(v1Row).getByRole("button", { name: "Restore" }));
    const restoreTitle3 = await screen.findByText("Restore version v1");
    const restoreDialog3 = restoreTitle3.closest("dialog") as HTMLElement;
    fireEvent.click(within(restoreDialog3).getByRole("button", { name: "Restore" }));
    await screen.findByText(/Restore blocked by active reservations/);

    fireEvent.click(within(restoreDialog3).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByText("Restore version v1")).not.toBeInTheDocument());

    // Reopening a restore target shows a clean dialog, no stale blocking list.
    fireEvent.click(within(v1Row).getByRole("button", { name: "Restore" }));
    expect(await screen.findByText("Restore version v1")).toBeInTheDocument();
    expect(screen.queryByText(/Restore blocked by active reservations/)).not.toBeInTheDocument();
  });

  it("History toggles closed when clicked a second time", async () => {
    server.use(...baseHandlers());
    await renderPageWithCanvas([deviceNode("n-live", "d-live")]);

    fireEvent.click(screen.getByRole("button", { name: "History" }));
    expect(await screen.findByLabelText("Version history panel")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "History" }));
    await waitFor(() =>
      expect(screen.queryByLabelText("Version history panel")).not.toBeInTheDocument(),
    );
  });

  it("the panel's own close button also closes it, distinct from the History toggle", async () => {
    server.use(...baseHandlers());
    await renderPageWithCanvas([deviceNode("n-live", "d-live")]);

    fireEvent.click(screen.getByRole("button", { name: "History" }));
    expect(await screen.findByLabelText("Version history panel")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Close history panel" }));
    await waitFor(() =>
      expect(screen.queryByLabelText("Version history panel")).not.toBeInTheDocument(),
    );
  });
});

describe("TopologyEditorPage Save-as-Template X close", () => {
  it("the Save-as-Template modal's own X close button clears the name and error, same as Cancel", async () => {
    server.use(
      ...baseHandlers(),
      http.post(`/api/cabling/templates/from-topology/${TOPO_ID}`, () =>
        HttpResponse.json({ detail: "Name already in use" }, { status: 409 }),
      ),
    );
    await renderPageWithCanvas([deviceNode("n-dev", "d-1")]);

    fireEvent.click(screen.getByRole("button", { name: "Save as Template" }));
    const nameInput = await screen.findByLabelText("Template name");
    fireEvent.change(nameInput, { target: { value: "Dup" } });
    fireEvent.click(within(templateForm()).getByRole("button", { name: "Save" }));
    await screen.findByText("Name already in use");

    const templateDialog = nameInput.closest("dialog") as HTMLDialogElement;
    fireEvent.click(within(templateDialog).getByRole("button", { name: "Close dialog" }));

    await waitFor(() => expect(templateDialog.open).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: "Save as Template" }));
    expect(await screen.findByLabelText("Template name")).toHaveValue("");
    expect(screen.queryByText("Name already in use")).not.toBeInTheDocument();
  });
});

describe("TopologyEditorPage toolbar handlers and misc branches", () => {
  it("Back navigates to the topology list", async () => {
    server.use(...baseHandlers());
    await renderPageWithCanvas([]);

    fireEvent.click(screen.getByRole("button", { name: "Back" }));

    expect(await screen.findByText("topology list page")).toBeInTheDocument();
  });

  it("clicking an edge layer button updates selectedEdgeLayer and its pressed state", async () => {
    server.use(...baseHandlers());
    await renderPageWithCanvas([]);

    const l1Button = screen.getByRole("button", { name: /^L1:/ });
    fireEvent.click(l1Button);

    expect(l1Button).toHaveAttribute("aria-pressed", "true");
    expect(useTopologyStore.getState().selectedEdgeLayer).toBe("L1");
  });

  it("Quick connect toggles aria-pressed", async () => {
    server.use(...baseHandlers());
    await renderPageWithCanvas([]);

    const button = screen.getByRole("button", { name: "Quick connect" });
    expect(button).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(button);
    expect(button).toHaveAttribute("aria-pressed", "true");
  });

  it("description input updates on change and clears after a successful save", async () => {
    server.use(
      ...baseHandlers(),
      http.put(`/api/cabling/topologies/${TOPO_ID}`, () => HttpResponse.json(PARENT_TOPOLOGY)),
    );
    await renderPageWithCanvas([deviceNode("n-dev", "d-1")]);

    const input = screen.getByLabelText("Version description");
    fireEvent.change(input, { target: { value: "a change" } });
    expect(input).toHaveValue("a change");

    // The toolbar's own Save button (not the Save-as-Template modal's, which
    // is not open here) is the only "Save" button on screen at this point.
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith("Topology saved"));
    expect(input).toHaveValue("");
  });

  it("Clear canvas confirm empties the store and cancel leaves it untouched", async () => {
    server.use(...baseHandlers());
    await renderPageWithCanvas([deviceNode("n-dev", "d-1")]);

    fireEvent.click(screen.getByRole("button", { name: "Clear canvas" }));
    expect(await screen.findByText("Clear canvas?")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(useTopologyStore.getState().nodes).toHaveLength(1);

    fireEvent.click(screen.getByRole("button", { name: "Clear canvas" }));
    fireEvent.click(screen.getByRole("button", { name: "Clear" }));
    expect(useTopologyStore.getState().nodes).toHaveLength(0);
  });

  it("reserve modal prefills initialDynamicEntries from the canvas placeholders present at the moment it is opened", async () => {
    server.use(...baseHandlers());
    await renderPageWithCanvas([placeholderNode("n-ph", "dt-1", 2)]);

    fireEvent.click(screen.getByRole("button", { name: /Reserve Topology/ }));
    expect(await screen.findByLabelText("Instance count 1")).toHaveValue(2);

    // The reserve modal is conditionally MOUNTED ({showReserveModal && <...>}),
    // not conditionally shown via an `open` prop: closing it unmounts the
    // component entirely, and CreateReservationModal's dynamicEntries state is
    // seeded from initialDynamicEntries through a lazy useState initializer,
    // which runs once per mount. So a second placeholder added after closing
    // is picked up the next time the modal is opened, because that open is a
    // fresh mount with the current dynamicPrefill, not a stale one.
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    act(() => {
      useTopologyStore.setState((s) => ({
        nodes: [...s.nodes, placeholderNode("n-ph-2", "dt-2", 5)],
      }));
    });

    fireEvent.click(screen.getByRole("button", { name: /Reserve Topology/ }));
    expect(await screen.findByLabelText("Instance count 1")).toHaveValue(2);
    expect(screen.getByLabelText("Instance count 2")).toHaveValue(5);
  });
});

describe("TopologyEditorPage isValidConnection branches", () => {
  it("returns false with no toast when either endpoint node is missing", async () => {
    server.use(...baseHandlers());
    await renderPageWithCanvas([deviceNode("n-dev", "d-1")]);

    const isValidConnection = rfProps.current?.isValidConnection as (c: {
      source: string;
      target: string;
    }) => boolean;

    expect(isValidConnection({ source: "n-dev", target: "does-not-exist" })).toBe(false);
    expect(isValidConnection({ source: "does-not-exist", target: "n-dev" })).toBe(false);
    // The missing-node guard returns false silently: no toast is the
    // behavior under test here (it is distinct from the mismatch and
    // dynamic-placeholder guards, which do toast).
    expect(toastError).not.toHaveBeenCalled();
  });

  it("refuses connecting two devices of different topology types with the exact toast text", async () => {
    server.use(...baseHandlers());
    await renderPageWithCanvas([
      deviceNode("n-phys", "d-phys", "PHYSICAL"),
      deviceNode("n-cloud", "d-cloud", "CLOUD"),
    ]);

    const isValidConnection = rfProps.current?.isValidConnection as (c: {
      source: string;
      target: string;
    }) => boolean;

    expect(isValidConnection({ source: "n-phys", target: "n-cloud" })).toBe(false);
    expect(toastError).toHaveBeenCalledWith(
      "Cannot connect PHYSICAL and CLOUD devices: topology types must match",
      { id: "topology-mismatch" },
    );
  });

  it("accepts connecting two devices of the same topology type", async () => {
    server.use(...baseHandlers());
    await renderPageWithCanvas([
      deviceNode("n-a", "d-a", "PHYSICAL"),
      deviceNode("n-b", "d-b", "PHYSICAL"),
    ]);

    const isValidConnection = rfProps.current?.isValidConnection as (c: {
      source: string;
      target: string;
    }) => boolean;

    expect(isValidConnection({ source: "n-a", target: "n-b" })).toBe(true);
    expect(toastError).not.toHaveBeenCalled();
  });
});

describe("TopologyEditorPage pathfind path-status reconcile", () => {
  it("reconciles pathValid and hopCount from a pathfind response onto matching edges", async () => {
    server.use(
      ...baseHandlers(),
      http.post("/api/cabling/pathfind/batch", () =>
        HttpResponse.json({
          results: [{ reachable: true, hop_count: 3, paths: [] }],
        }),
      ),
    );
    await renderPageWithCanvas([
      deviceNode("n-src", "d-src"),
      deviceNode("n-tgt", "d-tgt"),
    ]);
    act(() => {
      useTopologyStore.setState({
        nodes: useTopologyStore.getState().nodes,
        edges: [
          {
            id: "e1",
            source: "n-src",
            target: "n-tgt",
            type: "layerEdge",
            data: { layer: "L1", pathValid: null },
          },
        ],
      });
    });

    await waitFor(() => {
      const edge = useTopologyStore.getState().edges.find((e) => e.id === "e1");
      expect(edge?.data?.pathValid).toBe(true);
      expect(edge?.data?.pathHopCount).toBe(3);
    });
  });
});
