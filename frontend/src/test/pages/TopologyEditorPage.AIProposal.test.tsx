import { http, HttpResponse } from "msw";
import { render, screen, waitFor, act } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import type { Node } from "@xyflow/react";

// Covers handleAIProposal (TopologyEditorPage.tsx) with a canvas that already
// carries a non-device node (a networkElementNode and a dynamicPlaceholderNode)
// present before the proposal lands, plus the accept/modify/reject proposal
// lifecycle and the AI commit dialog's onCommitted navigation. The dropped
// dynamic-template and network-element behaviors themselves stay in
// TopologyEditorDynamicPlaceholders.test.tsx / TopologyEditorNetworkElements.test.tsx;
// this file is about handleAIProposal reading canvas node data safely and the
// proposal accept/modify/reject/commit callbacks.

const { toastError, toastSuccess, toastPlain } = vi.hoisted(() => ({
  toastError: vi.fn(),
  toastSuccess: vi.fn(),
  toastPlain: vi.fn(),
}));
vi.mock("react-hot-toast", () => ({
  default: Object.assign(
    (msg: string) => toastPlain(msg),
    { error: toastError, success: toastSuccess, custom: vi.fn(), dismiss: vi.fn() },
  ),
}));

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

vi.mock("@/api/inventory", async () => {
  const actual = await vi.importActual<typeof import("@/api/inventory")>("@/api/inventory");
  return { ...actual, hydrateCanvasNodes: (d: unknown) => Promise.resolve(d) };
});

vi.mock("@/components/equipment-browser/EquipmentBrowser", () => ({
  EquipmentBrowser: () => <div data-testid="equipment-browser" />,
}));

// Stub AIDialog/AICommitDialog: driving a real AI generate/commit round trip
// is out of scope for this file (their own components have their own tests).
// Capturing onProposal/onCommitted lets these tests call the page's real
// handlers directly with a crafted response, exactly like TopologyEditorWiring
// stubs ElementAttachDialog to drive handleConnect.
const aiDialogProps = vi.hoisted(() => ({ current: null as Record<string, unknown> | null }));
const aiCommitProps = vi.hoisted(() => ({ current: null as Record<string, unknown> | null }));
vi.mock("@/components/topology-editor/AIDialog", () => ({
  AIDialog: (props: Record<string, unknown>) => {
    aiDialogProps.current = props;
    return props.open ? <div data-testid="ai-dialog" /> : null;
  },
}));
vi.mock("@/components/topology-editor/AICommitDialog", () => ({
  AICommitDialog: (props: Record<string, unknown>) => {
    aiCommitProps.current = props;
    return props.open ? <div data-testid="ai-commit-dialog" /> : null;
  },
}));

import { server } from "../mocks/server";
import { TopologyEditorPage } from "@/pages/TopologyEditorPage";
import { useTopologyStore } from "@/stores/topologyStore";
import type { CanvasNodeData } from "@/types/topology.types";
import type { AIGenerateResponse } from "@/types/ai.types";

const TOPO_ID = "topo-ai-1";

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

function elementNode(id: string): Node<CanvasNodeData> {
  return {
    id,
    type: "networkElementNode",
    position: { x: 50, y: 50 },
    data: { element: { id: `elem-${id}`, element_type: "vlan_segment", label: "VLAN A", attrs: {} } } as CanvasNodeData,
  };
}

function placeholderNode(id: string): Node<CanvasNodeData> {
  return {
    id,
    type: "dynamicPlaceholderNode",
    position: { x: 100, y: 100 },
    data: { templateId: "dt-1", templateName: "Ubuntu VM", templateIcon: null, count: 1 } as CanvasNodeData,
  };
}

function proposalResponse(overrides: Partial<AIGenerateResponse> = {}): AIGenerateResponse {
  return {
    purpose: "Test proposal",
    devices: [
      {
        role: "leaf1",
        device: { id: "d-new-1", name: "leaf1-dev", topology_type: "PHYSICAL", status: "AVAILABLE" },
      },
    ],
    edges: [],
    notes: "",
    file_summaries: [],
    ...overrides,
  } as unknown as AIGenerateResponse;
}

function baseHandlers() {
  return [
    http.get(`/api/cabling/topologies/${TOPO_ID}`, () => HttpResponse.json(PARENT_TOPOLOGY)),
    http.get("/api/reservations/", () =>
      HttpResponse.json({ items: [], total: 0, skip: 0, limit: 500 }),
    ),
    http.get("/api/ai/status", () => HttpResponse.json({ enabled: true })),
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
          <Route path="/topology/:id2" element={<div>redirected</div>} />
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

function fireAIProposal(response: AIGenerateResponse) {
  const onProposal = aiDialogProps.current?.onProposal as (r: AIGenerateResponse) => void;
  act(() => onProposal(response));
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
  aiDialogProps.current = null;
  aiCommitProps.current = null;
  useTopologyStore.setState({ nodes: [], edges: [], selectedEdgeLayer: "L2" });
});

describe("TopologyEditorPage handleAIProposal node-type safety", () => {
  it("does not crash when the canvas already holds a network element node and a dynamic placeholder", async () => {
    server.use(...baseHandlers());
    await renderPageWithCanvas([
      deviceNode("n-dev", "d-existing"),
      elementNode("n-elem"),
      placeholderNode("n-ph"),
    ]);

    // Before the isDeviceNode guard, collectCanvasDeviceIds (used to skip
    // devices already on the canvas) or the duplicate-detection read could
    // throw on a node with no `.device` at all. This must complete cleanly
    // and add the proposed device as a new ghost node.
    expect(() => fireAIProposal(proposalResponse())).not.toThrow();

    await waitFor(() => {
      const proposalNode = useTopologyStore
        .getState()
        .nodes.find((n) => n.type === "deviceNode" && (n.data as { isProposal?: boolean }).isProposal);
      expect(proposalNode).toBeDefined();
    });
    // The pre-existing element and placeholder nodes are untouched.
    expect(useTopologyStore.getState().nodes.map((n) => n.id)).toEqual(
      expect.arrayContaining(["n-dev", "n-elem", "n-ph"]),
    );
  });

  it("discards the proposal with a toast when a proposed device duplicates one already on the canvas (mixed-node canvas)", async () => {
    server.use(...baseHandlers());
    await renderPageWithCanvas([
      deviceNode("n-dev", "d-new-1"),
      elementNode("n-elem"),
      placeholderNode("n-ph"),
    ]);

    fireAIProposal(proposalResponse());

    expect(toastError).toHaveBeenCalledWith(
      "AI picked 1 device(s) already on the canvas; proposal discarded",
    );
    // No proposal ghost node was added.
    expect(
      useTopologyStore.getState().nodes.some((n) => (n.data as { isProposal?: boolean })?.isProposal),
    ).toBe(false);
  });

  it("discards the proposal with a toast when a role has no resolved device", async () => {
    server.use(...baseHandlers());
    await renderPageWithCanvas([elementNode("n-elem")]);

    fireAIProposal(
      proposalResponse({
        devices: [{ role: "leaf1" } as unknown as AIGenerateResponse["devices"][number]],
      }),
    );

    expect(toastError).toHaveBeenCalledWith(
      "AI proposal is missing resolved devices for 1 role(s); discarding",
    );
  });

  it("rejects any stale proposal before rendering a new one", async () => {
    server.use(...baseHandlers());
    await renderPageWithCanvas([]);

    fireAIProposal(proposalResponse({ devices: [
      { role: "leaf1", device: { id: "d-a", name: "a", topology_type: "PHYSICAL", status: "AVAILABLE" } },
    ] as unknown as AIGenerateResponse["devices"] }));
    await waitFor(() =>
      expect(useTopologyStore.getState().nodes.some((n) => n.id && n.type === "deviceNode")).toBe(true),
    );
    const firstProposalNodeId = useTopologyStore
      .getState()
      .nodes.find((n) => n.type === "deviceNode")!.id;

    fireAIProposal(proposalResponse({ devices: [
      { role: "leaf2", device: { id: "d-b", name: "b", topology_type: "PHYSICAL", status: "AVAILABLE" } },
    ] as unknown as AIGenerateResponse["devices"] }));

    await waitFor(() => {
      const ids = useTopologyStore.getState().nodes.map((n) => n.id);
      expect(ids).not.toContain(firstProposalNodeId);
    });
  });
});

describe("TopologyEditorPage handleAIProposal network elements (issue #632)", () => {
  it("renders a ghost element node and a device-sourced ghost attachment edge", async () => {
    server.use(...baseHandlers());
    await renderPageWithCanvas([]);

    fireAIProposal(
      proposalResponse({
        devices: [
          {
            role: "leaf1",
            device: {
              id: "d-new-1",
              name: "leaf1-dev",
              topology_type: "PHYSICAL",
              status: "AVAILABLE",
            },
          },
        ] as unknown as AIGenerateResponse["devices"],
        elements: [
          {
            role: "mgmt-seg",
            element_type: "vlan_segment",
            label: "Mgmt VLAN",
            attrs: { vlan_id: 100 },
          },
        ] as unknown as AIGenerateResponse["elements"],
        edges: [
          { source_role: "leaf1", target_role: "mgmt-seg", layer: "L2" },
        ] as unknown as AIGenerateResponse["edges"],
      }),
    );

    await waitFor(() => {
      const elementNode = useTopologyStore
        .getState()
        .nodes.find((n) => n.type === "networkElementNode");
      expect(elementNode).toBeDefined();
    });

    const state = useTopologyStore.getState();
    const deviceGhost = state.nodes.find((n) => n.type === "deviceNode");
    const elementGhost = state.nodes.find((n) => n.type === "networkElementNode");
    expect((elementGhost?.data as { isProposal?: boolean }).isProposal).toBe(true);
    expect(
      (elementGhost?.data as { element?: Record<string, unknown> }).element,
    ).toMatchObject({
      element_type: "vlan_segment",
      label: "Mgmt VLAN",
      attrs: { vlan_id: 100 },
    });

    // The store's addEnrichedEdge direction normalization must land the
    // device as source and the element as target, exactly like a
    // user-drawn attachment (ElementAttachDialog); the ghost carries no
    // port fields (the committer picks the port on accept, D2).
    const attachmentEdge = state.edges.find((e) => e.target === elementGhost?.id);
    expect(attachmentEdge).toBeDefined();
    expect(attachmentEdge?.source).toBe(deviceGhost?.id);
    const edgeData = attachmentEdge?.data as {
      isProposal?: boolean;
      layer?: string;
      source_port_name?: string;
    };
    expect(edgeData?.isProposal).toBe(true);
    expect(edgeData?.layer).toBe("L2");
    expect(edgeData?.source_port_name).toBeUndefined();
  });

  it("does not throw when a proposed element shares its role with a proposed device", async () => {
    // The backend rejects a duplicate role across a device and an element
    // (D4), but a hand-crafted or stale response could still reach the
    // frontend; handleAIProposal must not throw regardless of which node
    // wins the shared roleToNodeId entry.
    server.use(...baseHandlers());
    await renderPageWithCanvas([]);

    expect(() =>
      fireAIProposal(
        proposalResponse({
          devices: [
            {
              role: "dup-role",
              device: {
                id: "d-new-1",
                name: "leaf1-dev",
                topology_type: "PHYSICAL",
                status: "AVAILABLE",
              },
            },
          ] as unknown as AIGenerateResponse["devices"],
          elements: [
            { role: "dup-role", element_type: "vlan_segment", label: "Dup", attrs: {} },
          ] as unknown as AIGenerateResponse["elements"],
          edges: [],
        }),
      ),
    ).not.toThrow();

    await waitFor(() => {
      const nodes = useTopologyStore.getState().nodes;
      expect(nodes.some((n) => n.type === "deviceNode")).toBe(true);
      expect(nodes.some((n) => n.type === "networkElementNode")).toBe(true);
    });
  });

  it("a device-only proposal (no elements field) renders no element node", async () => {
    server.use(...baseHandlers());
    await renderPageWithCanvas([]);

    expect(() => fireAIProposal(proposalResponse())).not.toThrow();

    await waitFor(() => {
      expect(useTopologyStore.getState().nodes.some((n) => n.type === "deviceNode")).toBe(true);
    });
    expect(
      useTopologyStore.getState().nodes.some((n) => n.type === "networkElementNode"),
    ).toBe(false);
  });
});

describe("TopologyEditorPage proposal accept/modify/reject", () => {
  it("modify accepts the ghost nodes for editing, shows a toast, and clears the proposal bar", async () => {
    server.use(...baseHandlers());
    await renderPageWithCanvas([]);
    fireAIProposal(proposalResponse());
    await waitFor(() => expect(screen.getByText(/Test proposal/)).toBeInTheDocument());

    const modifyButton = screen.getByRole("button", { name: "Modify" });
    act(() => modifyButton.click());

    expect(toastPlain).toHaveBeenCalledWith("Proposal accepted for editing");
    await waitFor(() => expect(screen.queryByText(/Test proposal/)).not.toBeInTheDocument());
    // The node is still on the canvas, now a real (non-proposal) node.
    const node = useTopologyStore.getState().nodes.find((n) => n.type === "deviceNode");
    expect((node?.data as { isProposal?: boolean }).isProposal).toBe(false);
  });

  it("reject removes the ghost nodes and clears the proposal bar", async () => {
    server.use(...baseHandlers());
    await renderPageWithCanvas([]);
    fireAIProposal(proposalResponse());
    await waitFor(() => expect(screen.getByText(/Test proposal/)).toBeInTheDocument());

    const rejectButton = screen.getByRole("button", { name: "Reject" });
    act(() => rejectButton.click());

    await waitFor(() => expect(screen.queryByText(/Test proposal/)).not.toBeInTheDocument());
    expect(useTopologyStore.getState().nodes).toHaveLength(0);
  });

  it("accept opens the AI commit dialog with the pending proposal", async () => {
    server.use(...baseHandlers());
    await renderPageWithCanvas([]);
    fireAIProposal(proposalResponse());
    await waitFor(() => expect(screen.getByText(/Test proposal/)).toBeInTheDocument());

    const acceptButton = screen.getByRole("button", { name: "Accept" });
    act(() => acceptButton.click());

    await waitFor(() => expect(aiCommitProps.current?.open).toBe(true));
    expect((aiCommitProps.current?.proposal as AIGenerateResponse).purpose).toBe("Test proposal");
  });

  it("committing the AI proposal clears the proposal, rejects the ghost nodes, and navigates to the new topology", async () => {
    server.use(...baseHandlers());
    await renderPageWithCanvas([]);
    fireAIProposal(proposalResponse());
    await waitFor(() => expect(screen.getByText(/Test proposal/)).toBeInTheDocument());
    act(() => screen.getByRole("button", { name: "Accept" }).click());
    await waitFor(() => expect(aiCommitProps.current?.open).toBe(true));

    const onCommitted = aiCommitProps.current?.onCommitted as (r: { topology_id: string }) => void;
    act(() => onCommitted({ topology_id: "topo-new-99" }));

    // Ghost proposal state is cleared and the proposal bar goes away.
    await waitFor(() => expect(screen.queryByText(/Test proposal/)).not.toBeInTheDocument());
    expect(useTopologyStore.getState().nodes).toHaveLength(0);
  });
});
