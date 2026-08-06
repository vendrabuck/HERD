import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import type { ReactNode } from "react";

import type { DynamicPlaceholderNodeData } from "@/types/topology.types";
import { useTopologyStore } from "@/stores/topologyStore";

// Mock the React Flow Handle primitive so the node renders as plain DOM
// without a ReactFlowProvider. We only assert on the node's own markup.
vi.mock("@xyflow/react", () => ({
  Handle: ({ children }: { children?: ReactNode }) => (
    <span data-testid="rf-handle">{children}</span>
  ),
  Position: { Top: "top", Right: "right", Bottom: "bottom", Left: "left" },
}));

import { DynamicPlaceholderNode } from "@/components/topology-editor/nodes/DynamicPlaceholderNode";

const NODE_ID = "ph-1";

function placeholderData(overrides: Partial<DynamicPlaceholderNodeData> = {}): DynamicPlaceholderNodeData {
  return {
    templateId: "dt-1",
    templateName: "Ubuntu VM",
    templateIcon: null,
    count: 1,
    ...overrides,
  };
}

function renderNode(data: DynamicPlaceholderNodeData) {
  const props = { id: NODE_ID, data, selected: false } as unknown as Parameters<
    typeof DynamicPlaceholderNode
  >[0];
  return render(<DynamicPlaceholderNode {...props} />);
}

function seedStore(data: DynamicPlaceholderNodeData) {
  useTopologyStore.setState({
    nodes: [
      { id: NODE_ID, type: "dynamicPlaceholderNode", position: { x: 0, y: 0 }, data },
    ],
    edges: [],
    selectedEdgeLayer: "L2",
  });
}

function storeCount(): number {
  const node = useTopologyStore.getState().nodes.find((n) => n.id === NODE_ID);
  return (node?.data as DynamicPlaceholderNodeData).count;
}

beforeEach(() => {
  useTopologyStore.setState({ nodes: [], edges: [], selectedEdgeLayer: "L2" });
});

describe("DynamicPlaceholderNode", () => {
  it("renders the template name, DYNAMIC tag, and dashed purple ghost styling", () => {
    const { container } = renderNode(placeholderData({ count: 3 }));

    expect(screen.getByText("Ubuntu VM")).toBeTruthy();
    expect(screen.getByText("DYNAMIC")).toBeTruthy();
    expect(screen.getByText("Created at activation")).toBeTruthy();
    expect(screen.getByLabelText("Instance count for Ubuntu VM")).toHaveValue(3);

    const node = container.firstElementChild as HTMLElement;
    expect(node.className).toContain("border-dashed");
    expect(node.className).toContain("border-purple-400");
    expect(node.className).toContain("bg-purple-50");
  });

  it("writes an edited count back to the store node", () => {
    seedStore(placeholderData({ count: 1 }));
    renderNode(placeholderData({ count: 1 }));

    fireEvent.change(screen.getByLabelText("Instance count for Ubuntu VM"), {
      target: { value: "5" },
    });
    expect(storeCount()).toBe(5);
  });

  it("clamps the count to the 1..50 backend bounds", () => {
    seedStore(placeholderData({ count: 5 }));
    renderNode(placeholderData({ count: 5 }));

    const input = screen.getByLabelText("Instance count for Ubuntu VM");

    fireEvent.change(input, { target: { value: "0" } });
    expect(storeCount()).toBe(1);

    fireEvent.change(input, { target: { value: "999" } });
    expect(storeCount()).toBe(50);
  });
});
