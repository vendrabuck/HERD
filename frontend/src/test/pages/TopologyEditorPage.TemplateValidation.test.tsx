import { http, HttpResponse } from "msw";
import { render, screen, fireEvent, act, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import type { Node } from "@xyflow/react";

// Issue #1314: the Save as template dialog's inline error state. Cabling
// refuses a name over 100 characters with FastAPI's default 422, whose detail
// is a LIST of objects; the old `detail?: string` cast stored that list in
// state and rendered it as a React child, which threw and blanked the app.
// The harness mirrors TopologyEditorPage.HistoryAndSave.test.tsx, kept in its
// own file so the editor's other suites stay untouched.

vi.mock("react-hot-toast", () => ({
  default: Object.assign(vi.fn(), {
    error: vi.fn(),
    success: vi.fn(),
    custom: vi.fn(),
    dismiss: vi.fn(),
  }),
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

import { server } from "../mocks/server";
import { TopologyEditorPage } from "@/pages/TopologyEditorPage";
import { ErrorBoundary } from "@/components/ui/ErrorBoundary";
import { useTopologyStore } from "@/stores/topologyStore";
import type { CanvasNodeData } from "@/types/topology.types";

const TOPO_ID = "topo-tpl-422";

const TOPOLOGY = {
  id: TOPO_ID,
  name: "Validation topology",
  created_by: "u",
  owner_name: "u",
  created_at: "2026-05-01T00:00:00Z",
  updated_at: "2026-05-01T00:00:00Z",
  canvas_data: null,
};

// The body cabling answers for a 101-character name: FastAPI's default
// RequestValidationError shape, verbatim keys.
const LONG_NAME = "n".repeat(101);
const VALIDATION_422 = {
  detail: [
    {
      type: "string_too_long",
      loc: ["body", "name"],
      msg: "String should have at most 100 characters",
      input: LONG_NAME,
      ctx: { max_length: 100 },
    },
  ],
};

function baseHandlers() {
  return [
    http.get(`/api/cabling/topologies/${TOPO_ID}`, () => HttpResponse.json(TOPOLOGY)),
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
  ];
}

function deviceNode(id: string): Node<CanvasNodeData> {
  return {
    id,
    type: "deviceNode",
    position: { x: 0, y: 0 },
    data: {
      device: { id, name: id, topology_type: "PHYSICAL", status: "AVAILABLE" },
      label: id,
      topologyType: "PHYSICAL",
    } as CanvasNodeData,
  };
}

async function renderEditor() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[`/topology/${TOPO_ID}`]}>
        {/* The root boundary, so a render throw shows up as its alert. */}
        <ErrorBoundary>
          <Routes>
            <Route path="/topology/:id" element={<TopologyEditorPage />} />
          </Routes>
        </ErrorBoundary>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  await screen.findByText("Validation topology");
  act(() => {
    useTopologyStore.setState({ nodes: [deviceNode("d-1")], edges: [], selectedEdgeLayer: "L2" });
  });
}

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
  useTopologyStore.setState({ nodes: [], edges: [], selectedEdgeLayer: "L2" });
});

describe("TopologyEditorPage Save as template validation (issue #1314)", () => {
  it("caps the name input at the 100-character limit of cabling", async () => {
    server.use(...baseHandlers());
    await renderEditor();
    fireEvent.click(screen.getByRole("button", { name: "Save as Template" }));
    expect(await screen.findByLabelText("Template name")).toHaveAttribute("maxLength", "100");
  });

  it("shows a 422 validation list as readable text and the editor stays mounted", async () => {
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
    server.use(
      ...baseHandlers(),
      http.post(`/api/cabling/templates/from-topology/${TOPO_ID}`, () =>
        HttpResponse.json(VALIDATION_422, { status: 422 }),
      ),
    );
    await renderEditor();

    fireEvent.click(screen.getByRole("button", { name: "Save as Template" }));
    // maxLength stops typing, not a programmatic value; the server is the
    // judge either way, and this is the path a pasted or scripted name takes.
    fireEvent.change(await screen.findByLabelText("Template name"), {
      target: { value: LONG_NAME },
    });
    fireEvent.click(within(templateForm()).getByRole("button", { name: "Save" }));

    expect(
      await within(templateForm()).findByText("name: String should have at most 100 characters"),
    ).toBeInTheDocument();
    // No boundary tripped: the editor and its toolbar are still there.
    expect(screen.queryByText("Something went wrong")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save as Template" })).toBeInTheDocument();
    expect(screen.getByLabelText("Template name")).toHaveValue(LONG_NAME);
    // The echoed input is never shown.
    expect(within(templateForm()).queryByText(new RegExp("n{101}"))).toBeNull();
    consoleError.mockRestore();
  });
});
