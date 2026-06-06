import { render, screen } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import type { CSSProperties, ReactNode } from "react";

// Mock React Flow primitives so we can render LayerEdge as plain DOM and
// assert directly on the props that drive the rendered cable.
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

import { LayerEdge } from "@/components/topology-editor/edges/LayerEdge";

function renderEdge(data: Record<string, unknown>) {
  const props = {
    id: "e1",
    sourceX: 0,
    sourceY: 0,
    targetX: 100,
    targetY: 0,
    sourcePosition: "right",
    targetPosition: "left",
    data,
  } as unknown as Parameters<typeof LayerEdge>[0];
  return render(<LayerEdge {...props} />);
}

describe("LayerEdge", () => {
  it("uses a thicker stroke (3px) so cables read clearly on the canvas", () => {
    renderEdge({ layer: "L2" });
    expect(screen.getByTestId("edge").getAttribute("data-stroke-width")).toBe("3");
  });

  it("renders red on any layer when pathValid is false", () => {
    renderEdge({ layer: "L2", pathValid: false });
    expect(screen.getByTestId("edge").getAttribute("data-stroke")).toBe("#ef4444");
    expect(screen.getByText("no path")).toBeTruthy();
  });

  it("renders red on L3 too when pathValid is false", () => {
    renderEdge({ layer: "L3", pathValid: false });
    expect(screen.getByTestId("edge").getAttribute("data-stroke")).toBe("#ef4444");
  });

  it("renders green on L2 when pathValid is true", () => {
    renderEdge({ layer: "L2", pathValid: true, pathHopCount: 3 });
    expect(screen.getByTestId("edge").getAttribute("data-stroke")).toBe("#22c55e");
    expect(screen.getByText("3 hops")).toBeTruthy();
  });

  it("falls back to the L3 base color when pathValid is unresolved", () => {
    renderEdge({ layer: "L3" });
    expect(screen.getByTestId("edge").getAttribute("data-stroke")).toBe("#22c55e");
  });

  it("portsCabled=false reds the edge regardless of pathValid", () => {
    renderEdge({ layer: "L2", pathValid: true, portsCabled: false });
    expect(screen.getByTestId("edge").getAttribute("data-stroke")).toBe("#ef4444");
    expect(screen.getByText("uncabled port")).toBeTruthy();
  });
});
