import { render, screen, fireEvent, act } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import type { ReactNode } from "react";

import type { NetworkElementNodeData } from "@/types/topology.types";
import { useTopologyStore } from "@/stores/topologyStore";

// Mock the React Flow Handle primitive so the node renders as plain DOM
// without a ReactFlowProvider, same pattern as DynamicPlaceholderNode.test.tsx.
vi.mock("@xyflow/react", () => ({
  Handle: ({ children }: { children?: ReactNode }) => (
    <span data-testid="rf-handle">{children}</span>
  ),
  Position: { Top: "top", Right: "right", Bottom: "bottom", Left: "left" },
}));

import { NetworkElementNode } from "@/components/topology-editor/nodes/NetworkElementNode";

const NODE_ID = "el-1";

function elementData(overrides: Partial<NetworkElementNodeData["element"]> = {}): NetworkElementNodeData {
  return {
    element: {
      id: "elem-uuid-1",
      element_type: "vlan_segment",
      label: "VLAN segment",
      attrs: {},
      ...overrides,
    },
  };
}

function renderNode(data: NetworkElementNodeData) {
  const props = { id: NODE_ID, data, selected: false } as unknown as Parameters<
    typeof NetworkElementNode
  >[0];
  return render(<NetworkElementNode {...props} />);
}

function seedStore(data: NetworkElementNodeData) {
  useTopologyStore.setState({
    nodes: [{ id: NODE_ID, type: "networkElementNode", position: { x: 0, y: 0 }, data }],
    edges: [],
    selectedEdgeLayer: "L2",
  });
}

function storeLabel(): string {
  const node = useTopologyStore.getState().nodes.find((n) => n.id === NODE_ID);
  return (node?.data as NetworkElementNodeData).element.label;
}

beforeEach(() => {
  useTopologyStore.setState({ nodes: [], edges: [], selectedEdgeLayer: "L2" });
});

describe("NetworkElementNode", () => {
  it("renders the label and dashed NEUTRAL GRAY styling, distinct from the placeholder's purple", () => {
    const { container } = renderNode(elementData({ label: "Mgmt VLAN" }));

    expect(screen.getByText("Mgmt VLAN")).toBeTruthy();

    const node = container.firstElementChild as HTMLElement;
    expect(node.className).toContain("border-dashed");
    expect(node.className).toContain("border-gray-400");
    expect(node.className).toContain("bg-gray-100");
    // Never the placeholder's purple palette: the two ephemeral-looking node
    // kinds must not be confusable (ADR 0012 "Canvas shape").
    expect(node.className).not.toContain("purple");
  });

  it("renders exactly one target handle and no source handle", () => {
    renderNode(elementData());
    expect(screen.getAllByTestId("rf-handle")).toHaveLength(1);
  });

  it("shows a per-type label for each of the four element types", () => {
    const cases: Array<[NetworkElementNodeData["element"]["element_type"], string]> = [
      ["vlan_segment", "VLAN segment"],
      ["subnet", "Subnet"],
      ["external_cloud", "External cloud"],
      ["patch_trunk", "Patch trunk"],
    ];
    for (const [element_type, expectedLabel] of cases) {
      const { unmount } = renderNode(elementData({ element_type, label: `label-${element_type}` }));
      expect(screen.getByText(expectedLabel)).toBeInTheDocument();
      unmount();
    }
  });

  it("double-clicking the label opens an inline edit field pre-filled with the current label", () => {
    renderNode(elementData({ label: "Original" }));
    fireEvent.doubleClick(screen.getByText("Original"));
    expect(screen.getByLabelText(/Label for VLAN segment/)).toHaveValue("Original");
  });

  it("committing an edited label (Enter) writes it back to the store node", () => {
    // The node's `data` prop is a static snapshot in this test harness (React
    // Flow itself is mocked out); a real canvas re-renders the node from the
    // store on the next tick. So this asserts the STORE write, the same
    // contract DynamicPlaceholderNode's count-edit test asserts.
    seedStore(elementData({ label: "Original" }));
    renderNode(elementData({ label: "Original" }));

    fireEvent.doubleClick(screen.getByText("Original"));
    const input = screen.getByLabelText(/Label for VLAN segment/);
    fireEvent.change(input, { target: { value: "Renamed segment" } });
    fireEvent.keyDown(input, { key: "Enter" });

    expect(storeLabel()).toBe("Renamed segment");
  });

  it("committing on blur also writes the edited label", () => {
    seedStore(elementData({ label: "Original" }));
    renderNode(elementData({ label: "Original" }));

    fireEvent.doubleClick(screen.getByText("Original"));
    const input = screen.getByLabelText(/Label for VLAN segment/);
    fireEvent.change(input, { target: { value: "Blurred rename" } });
    fireEvent.blur(input);

    expect(storeLabel()).toBe("Blurred rename");
  });

  it("Escape cancels the edit and leaves the store label unchanged", () => {
    seedStore(elementData({ label: "Original" }));
    renderNode(elementData({ label: "Original" }));

    fireEvent.doubleClick(screen.getByText("Original"));
    const input = screen.getByLabelText(/Label for VLAN segment/);
    fireEvent.change(input, { target: { value: "Should not stick" } });
    fireEvent.keyDown(input, { key: "Escape" });

    expect(screen.getByText("Original")).toBeInTheDocument();
    expect(storeLabel()).toBe("Original");
  });

  it("committing an empty/whitespace label falls back to the previous label instead of blanking it", () => {
    seedStore(elementData({ label: "Original" }));
    renderNode(elementData({ label: "Original" }));

    fireEvent.doubleClick(screen.getByText("Original"));
    const input = screen.getByLabelText(/Label for VLAN segment/);
    fireEvent.change(input, { target: { value: "   " } });
    fireEvent.keyDown(input, { key: "Enter" });

    expect(storeLabel()).toBe("Original");
  });

  it("renders a PROPOSED tag when isProposal is set", () => {
    renderNode({ ...elementData(), isProposal: true });
    expect(screen.getByText("PROPOSED")).toBeInTheDocument();
  });

  // Enter/Escape both end the edit by flipping `editing` to false, which
  // unmounts the input; in a real browser, removing a focused element from
  // the DOM fires a native blur on it as part of that removal, and since
  // React's synthetic onBlur is still wired to the node at that instant, it
  // re-invokes onBlur={commit} from a stale closure. jsdom does not
  // synthesize that blur on unmount by itself (a known jsdom/browser gap,
  // see CLAUDE.md's canvas-UI note), so these tests reproduce the same
  // ordering explicitly: batching the key event and the blur into one
  // `act()` call delivers both to the still-mounted input before React
  // commits the unmount, exactly as the browser's same-tick sequence would.
  // Firing them as two separate fireEvent calls (React's default automatic
  // batching flushes between them) does NOT reproduce the bug: the input is
  // already disconnected by the time blur fires, so the earlier version of
  // this test passed even against the unfixed component.
  it("Escape then blur does not write the cancelled draft to the store", () => {
    seedStore(elementData({ label: "Original" }));
    renderNode(elementData({ label: "Original" }));

    fireEvent.doubleClick(screen.getByText("Original"));
    const input = screen.getByLabelText(/Label for VLAN segment/);
    fireEvent.change(input, { target: { value: "Should not stick" } });

    act(() => {
      fireEvent.keyDown(input, { key: "Escape" });
      fireEvent.blur(input);
    });

    expect(storeLabel()).toBe("Original");
  });

  it("Enter then blur commits exactly once", () => {
    seedStore(elementData({ label: "Original" }));
    renderNode(elementData({ label: "Original" }));
    const setNetworkElementLabel = vi.spyOn(useTopologyStore.getState(), "setNetworkElementLabel");

    fireEvent.doubleClick(screen.getByText("Original"));
    const input = screen.getByLabelText(/Label for VLAN segment/);
    fireEvent.change(input, { target: { value: "Renamed once" } });

    act(() => {
      fireEvent.keyDown(input, { key: "Enter" });
      fireEvent.blur(input);
    });

    expect(storeLabel()).toBe("Renamed once");
    expect(setNetworkElementLabel).toHaveBeenCalledTimes(1);
  });
});
