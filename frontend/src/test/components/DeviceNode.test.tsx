import { render, screen } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import type { ReactNode } from "react";

// Mock React Flow primitives so DeviceNode renders as plain DOM without a
// ReactFlowProvider. We only need its visual shell (the colored wrapper and
// the device label), not real handle wiring.
vi.mock("@xyflow/react", () => ({
  Handle: () => null,
  Position: { Top: "top", Right: "right", Bottom: "bottom", Left: "left" },
}));

// TopoBadge pulls in cn/tailwind-merge; render it as a passthrough so this test
// stays focused on the DeviceNode wrapper colors and the name label.
vi.mock("@/components/ui/TopoBadge", () => ({
  TopoBadge: ({ type }: { type: string }): ReactNode => (
    <span data-testid="topo-badge">{type}</span>
  ),
}));

import { DeviceNode } from "@/components/topology-editor/nodes/DeviceNode";
import type { Device } from "@/types/device.types";

function makeDevice(overrides: Partial<Device> = {}): Device {
  return {
    id: "dev-1",
    name: "spine-01",
    template_id: "tpl-1",
    template_name: "Arista 7050",
    template_icon: null,
    template_vendor: null,
    template_model: null,
    template_part_number: null,
    topology_type: "PHYSICAL",
    status: "AVAILABLE",
    field_data: {},
    exclusive: false,
    driver_id: null,
    driver_name: null,
    connection_type: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    created_by: null,
    created_by_name: null,
    modified_by: null,
    modified_by_name: null,
    poll_interval_seconds: null,
    resolved_poll_interval_seconds: null,
    ...overrides,
  };
}

function renderNode(device: Device) {
  const props = {
    id: device.id,
    data: { device, label: device.name, topologyType: device.topology_type },
    selected: false,
  } as unknown as Parameters<typeof DeviceNode>[0];
  return render(<DeviceNode {...props} />);
}

describe("DeviceNode color regression guard (issue #106)", () => {
  it("renders the device name text in the DOM", () => {
    renderNode(makeDevice({ name: "spine-01" }));
    expect(screen.getByText("spine-01")).toBeTruthy();
  });

  it("applies the PHYSICAL color classes (bg-blue-100 / text-blue-900) to the node wrapper", () => {
    renderNode(makeDevice({ name: "phys-1", topology_type: "PHYSICAL" }));
    const wrapper = screen.getByText("phys-1").closest("div.relative");
    expect(wrapper).not.toBeNull();
    expect(wrapper!.className).toContain("bg-blue-100");
    expect(wrapper!.className).toContain("border-blue-400");
    expect(wrapper!.className).toContain("text-blue-900");
  });

  it("applies the CLOUD color classes (bg-purple-100 / text-purple-900) to the node wrapper", () => {
    renderNode(makeDevice({ name: "cloud-1", topology_type: "CLOUD" }));
    const wrapper = screen.getByText("cloud-1").closest("div.relative");
    expect(wrapper).not.toBeNull();
    expect(wrapper!.className).toContain("bg-purple-100");
    expect(wrapper!.className).toContain("border-purple-400");
    expect(wrapper!.className).toContain("text-purple-900");
  });
});
