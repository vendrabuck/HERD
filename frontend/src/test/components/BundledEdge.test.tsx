import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import type { CSSProperties, ReactNode } from "react";

// Same React Flow primitive mock LayerEdge.test.tsx uses: render as plain DOM
// and assert on the props that drive the rendered cable/badge.
vi.mock("@xyflow/react", () => ({
  BaseEdge: ({ style }: { style: CSSProperties }) => (
    <div
      data-testid="edge"
      data-stroke={String(style.stroke)}
      data-stroke-width={String(style.strokeWidth)}
    />
  ),
  EdgeLabelRenderer: ({ children }: { children: ReactNode }) => <>{children}</>,
  getBezierPath: () => ["M0,0", 0, 0, 0, 0],
}));

const mockRemoveEdge = vi.fn();
vi.mock("@/stores/topologyStore", () => ({
  useTopologyStore: (selector: (s: { removeEdge: typeof mockRemoveEdge }) => unknown) =>
    selector({ removeEdge: mockRemoveEdge }),
}));

import { BundledEdge } from "@/components/topology-editor/edges/BundledEdge";
import { groupEdgesForRender } from "@/components/topology-editor/edges/groupEdgesForRender";
import type { Edge } from "@xyflow/react";
import type { LayerEdgeData } from "@/types/topology.types";

function renderBundled(
  members: Array<{ id: string; data: LayerEdgeData | undefined }>,
  isReadOnly = false,
) {
  const props = {
    id: "bundle-a::b",
    source: "a",
    target: "b",
    sourceX: 0,
    sourceY: 0,
    targetX: 100,
    targetY: 0,
    sourcePosition: "right",
    targetPosition: "left",
    data: { members, isReadOnly },
  } as unknown as Parameters<typeof BundledEdge>[0];
  return render(<BundledEdge {...props} />);
}

function edge(
  id: string,
  source: string,
  target: string,
  overrides: Partial<LayerEdgeData> & {
    sourceHandle?: string | null;
    targetHandle?: string | null;
    selected?: boolean;
    animated?: boolean;
  } = {},
): Edge<LayerEdgeData> {
  const { sourceHandle, targetHandle, selected, animated, ...data } = overrides;
  return {
    id,
    source,
    target,
    sourceHandle: sourceHandle ?? null,
    targetHandle: targetHandle ?? null,
    selected,
    animated,
    type: "layerEdge",
    data: { layer: "L2", ...data },
  };
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("groupEdgesForRender", () => {
  it("a single edge between a device pair passes through unchanged as a layerEdge", () => {
    const { renderEdges, bundleMembers } = groupEdgesForRender([edge("e1", "a", "b")]);
    expect(renderEdges).toHaveLength(1);
    expect(renderEdges[0].type).toBe("layerEdge");
    expect(renderEdges[0].id).toBe("e1");
    expect(bundleMembers.size).toBe(0);
  });

  it("two or more edges sharing an unordered device pair collapse into one bundledEdge", () => {
    const { renderEdges } = groupEdgesForRender([
      edge("e1", "a", "b"),
      edge("e2", "a", "b"),
      edge("e3", "b", "a"), // reversed source/target, same unordered pair
    ]);
    expect(renderEdges).toHaveLength(1);
    expect(renderEdges[0].type).toBe("bundledEdge");
    const data = renderEdges[0].data as { members: Array<{ id: string }> };
    expect(data.members.map((m) => m.id).sort()).toEqual(["e1", "e2", "e3"]);
  });

  it("does not mutate or drop the underlying edges: grouping is render-only", () => {
    const storeEdges = [edge("e1", "a", "b"), edge("e2", "a", "b")];
    const { renderEdges } = groupEdgesForRender(storeEdges);
    // The store array itself is untouched: still 2 distinct edges, unique ids.
    expect(storeEdges).toHaveLength(2);
    expect(new Set(storeEdges.map((e) => e.id)).size).toBe(2);
    // The render view collapsed them into one visual bundle.
    expect(renderEdges).toHaveLength(1);
  });

  it("keeps proposal edges and edges for other device pairs rendered individually", () => {
    const proposal: Edge<LayerEdgeData> = {
      id: "p1",
      source: "a",
      target: "b",
      type: "layerEdge",
      data: { layer: "L1", isProposal: true },
    };
    const { renderEdges } = groupEdgesForRender([
      edge("e1", "a", "b"),
      edge("e2", "a", "b"),
      proposal,
      edge("e4", "c", "d"),
    ]);
    expect(renderEdges.find((r) => r.id === "p1")).toBeTruthy();
    expect(renderEdges.find((r) => r.id === "e4")?.type).toBe("layerEdge");
    expect(renderEdges.find((r) => r.type === "bundledEdge")).toBeTruthy();
  });

  it("returns a bundleId to member-ids map for a bundled pair (review item 3)", () => {
    const { renderEdges, bundleMembers } = groupEdgesForRender([
      edge("e1", "a", "b"),
      edge("e2", "a", "b"),
    ]);
    const bundleId = renderEdges[0].id;
    expect(bundleMembers.get(bundleId)?.sort()).toEqual(["e1", "e2"]);
  });

  it("carries the first member's sourceHandle/targetHandle onto the bundle (review item 4a)", () => {
    const { renderEdges } = groupEdgesForRender([
      edge("e1", "a", "b", { sourceHandle: "right", targetHandle: "left" }),
      edge("e2", "a", "b", { sourceHandle: "bottom", targetHandle: "top" }),
    ]);
    expect(renderEdges[0].sourceHandle).toBe("right");
    expect(renderEdges[0].targetHandle).toBe("left");
  });

  it("threads isReadOnly into the bundle's data so BundledEdge can gate its per-member delete (review round 3 item 3)", () => {
    const readOnly = groupEdgesForRender([edge("e1", "a", "b"), edge("e2", "a", "b")], true);
    const readOnlyData = readOnly.renderEdges[0].data as { isReadOnly: boolean };
    expect(readOnlyData.isReadOnly).toBe(true);

    const editable = groupEdgesForRender([edge("e1", "a", "b"), edge("e2", "a", "b")], false);
    const editableData = editable.renderEdges[0].data as { isReadOnly: boolean };
    expect(editableData.isReadOnly).toBe(false);
  });

  it("projects selected true onto the bundle when ANY member is selected, so React Flow's controlled reconciliation can see it (review item 1)", () => {
    const { renderEdges } = groupEdgesForRender([
      edge("e1", "a", "b", { selected: false }),
      edge("e2", "a", "b", { selected: true }),
    ]);
    expect(renderEdges[0].selected).toBe(true);
  });

  it("projects selected false when no member is selected", () => {
    const { renderEdges } = groupEdgesForRender([edge("e1", "a", "b"), edge("e2", "a", "b")]);
    expect(renderEdges[0].selected).toBe(false);
  });

  it("projects animated the same way, OR across members", () => {
    const { renderEdges } = groupEdgesForRender([
      edge("e1", "a", "b", { animated: false }),
      edge("e2", "a", "b", { animated: true }),
    ]);
    expect(renderEdges[0].animated).toBe(true);
  });

  it("does not throw when a member has no data at all (review item 3)", () => {
    const withDataless = [
      edge("e1", "a", "b"),
      { id: "e2", source: "a", target: "b", sourceHandle: null, targetHandle: null, type: "layerEdge" } as Edge<LayerEdgeData>,
    ];
    expect(() => groupEdgesForRender(withDataless)).not.toThrow();
    const { renderEdges } = groupEdgesForRender(withDataless);
    const data = renderEdges[0].data as { members: Array<{ id: string; data: LayerEdgeData | undefined }> };
    expect(data.members.find((m) => m.id === "e2")?.data).toBeUndefined();
  });
});

describe("BundledEdge", () => {
  it("renders a thick neutral stroke when every member is valid", () => {
    renderBundled([
      { id: "e1", data: { layer: "L1", source_port_name: "eth1", target_port_name: "0/0/1" } },
      { id: "e2", data: { layer: "L2", source_port_name: "eth2", target_port_name: "0/0/2" } },
    ]);
    const edgeEl = screen.getByTestId("edge");
    expect(edgeEl.getAttribute("data-stroke")).toBe("#4b5563");
    expect(edgeEl.getAttribute("data-stroke-width")).toBe("3");
  });

  it("renders red the moment any member is invalid (uncabled port), never averaging it away (review item 4b)", () => {
    renderBundled([
      { id: "e1", data: { layer: "L1", portsCabled: true } },
      { id: "e2", data: { layer: "L2", portsCabled: false } },
    ]);
    expect(screen.getByTestId("edge").getAttribute("data-stroke")).toBe("#ef4444");
  });

  it("renders red when any member has pathValid false, even if portsCabled is true", () => {
    renderBundled([
      { id: "e1", data: { layer: "L1", portsCabled: true, pathValid: true } },
      { id: "e2", data: { layer: "L2", portsCabled: true, pathValid: false } },
    ]);
    expect(screen.getByTestId("edge").getAttribute("data-stroke")).toBe("#ef4444");
  });

  it("shows the connection count in the badge", () => {
    renderBundled([
      { id: "e1", data: { layer: "L1" } },
      { id: "e2", data: { layer: "L2" } },
      { id: "e3", data: { layer: "L3" } },
    ]);
    expect(screen.getByText("3")).toBeInTheDocument();
    expect(screen.getByText("connections")).toBeInTheDocument();
  });

  it("does not throw and defaults a data-less member to L2 in the expanded list (review item 3)", () => {
    expect(() =>
      renderBundled([
        { id: "e1", data: { layer: "L2", source_port_name: "eth1", target_port_name: "0/0/1" } },
        { id: "e2", data: undefined },
      ]),
    ).not.toThrow();
    fireEvent.click(screen.getByRole("button", { name: /connections/ }));
    expect(screen.getByText("? to ?")).toBeInTheDocument();
    expect(screen.getAllByText("L2")).toHaveLength(2);
  });

  it("clicking the badge expands the per-connection list, each with its own status label; clicking again collapses it", () => {
    renderBundled([
      {
        id: "e1",
        data: { layer: "L1", source_port_name: "eth1", target_port_name: "0/0/1", portsCabled: false },
      },
      {
        id: "e2",
        data: { layer: "L3", source_port_name: "eth2", target_port_name: "0/0/2", pathValid: true, pathHopCount: 2 },
      },
    ]);
    expect(screen.queryByText("eth1 to 0/0/1")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /connections/ }));
    expect(screen.getByText("eth1 to 0/0/1")).toBeInTheDocument();
    expect(screen.getByText("eth2 to 0/0/2")).toBeInTheDocument();
    expect(screen.getByText("L1")).toBeInTheDocument();
    expect(screen.getByText("L3")).toBeInTheDocument();
    // The invalid member's status label survives being folded into the bundle.
    expect(screen.getByText("uncabled port")).toBeInTheDocument();
    expect(screen.getByText("2 hops")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /connections/ }));
    expect(screen.queryByText("eth1 to 0/0/1")).not.toBeInTheDocument();
  });

  it("per-member delete removes just that one connection from the store (review item 2)", () => {
    renderBundled([
      { id: "e1", data: { layer: "L1", source_port_name: "eth1", target_port_name: "0/0/1" } },
      { id: "e2", data: { layer: "L2", source_port_name: "eth2", target_port_name: "0/0/2" } },
    ]);
    fireEvent.click(screen.getByRole("button", { name: /connections/ }));

    fireEvent.click(screen.getByLabelText("Remove eth1 to 0/0/1"));

    expect(mockRemoveEdge).toHaveBeenCalledTimes(1);
    expect(mockRemoveEdge).toHaveBeenCalledWith("e1");
    // The other member's own delete control is untouched/unaffected.
    expect(screen.getByLabelText("Remove eth2 to 0/0/2")).toBeInTheDocument();
  });

  it("hides the per-member delete control in read-only mode, closing the store-bypass (review round 3 item 3)", () => {
    renderBundled(
      [
        { id: "e1", data: { layer: "L1", source_port_name: "eth1", target_port_name: "0/0/1" } },
        { id: "e2", data: { layer: "L2", source_port_name: "eth2", target_port_name: "0/0/2" } },
      ],
      true,
    );
    fireEvent.click(screen.getByRole("button", { name: /connections/ }));

    expect(screen.queryByLabelText("Remove eth1 to 0/0/1")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Remove eth2 to 0/0/2")).not.toBeInTheDocument();
    expect(mockRemoveEdge).not.toHaveBeenCalled();
  });
});
