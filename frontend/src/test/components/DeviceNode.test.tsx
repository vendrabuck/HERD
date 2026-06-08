import { render, screen } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import type { ReactNode } from "react";

import type { Device } from "@/types/device.types";
import type { DeviceNodeData } from "@/types/topology.types";

// Mock the React Flow Handle primitive so DeviceNode renders as plain DOM
// without a ReactFlowProvider. We only assert on DeviceNode's own markup.
vi.mock("@xyflow/react", () => ({
  Handle: ({ children }: { children?: ReactNode }) => <span data-testid="rf-handle">{children}</span>,
  Position: { Top: "top", Right: "right", Bottom: "bottom", Left: "left" },
}));

import { DeviceNode } from "@/components/topology-editor/nodes/DeviceNode";

const device = (overrides: Partial<Device> = {}): Device =>
  ({
    id: "dev-1",
    name: "L1-Edge-01",
    template_id: "tpl-1",
    template_name: "L1 Switch",
    template_icon: null,
    topology_type: "PHYSICAL",
    status: "AVAILABLE",
    field_data: {},
    ...overrides,
  }) as Device;

function renderNode(data: DeviceNodeData) {
  const props = { data, selected: false } as unknown as Parameters<typeof DeviceNode>[0];
  return render(<DeviceNode {...props} />);
}

describe("DeviceNode", () => {
  it("renders the device name and applies the PHYSICAL color + cursor-grab classes", () => {
    const { container } = renderNode({
      device: device(),
      label: "L1-Edge-01",
      topologyType: "PHYSICAL",
    });

    // Name text is present (the seeded-node bug rendered an empty span here).
    expect(screen.getByText("L1-Edge-01")).toBeTruthy();
    // Template name subtitle.
    expect(screen.getByText("L1 Switch")).toBeTruthy();

    const node = container.firstElementChild as HTMLElement;
    // The grab cursor wrapper that proves DeviceNode rendered (absent on the
    // React Flow default node, which was the #108 symptom).
    expect(node.className).toContain("cursor-grab");
    // PHYSICAL maps to the blue color class, not the gray fallback.
    expect(node.className).toContain("bg-blue-100");
    expect(node.className).not.toContain("bg-gray-100");
  });

  it("falls back to a gray color and surfaces a non-AVAILABLE status", () => {
    const { container } = renderNode({
      // topology_type outside the known map exercises the color fallback.
      device: device({ topology_type: "UNKNOWN" as Device["topology_type"], status: "OFFLINE" }),
      label: "L1-Edge-01",
      topologyType: "PHYSICAL",
    });

    const node = container.firstElementChild as HTMLElement;
    expect(node.className).toContain("bg-gray-100");
    expect(screen.getByText("OFFLINE")).toBeTruthy();
  });
});
