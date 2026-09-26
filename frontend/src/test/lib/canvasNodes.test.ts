import { describe, it, expect } from "vitest";
import type { Node } from "@xyflow/react";

import { collectCanvasDeviceIds, persistableDevice, persistableCanvasNodes } from "@/lib/canvasNodes";
import type { CanvasNodeData, DeviceNodeData, NetworkElementNodeData } from "@/types/topology.types";
import type { Device } from "@/types/device.types";

// Review fix (crash in TopologyEditorPage's handleAIProposal): a
// networkElementNode has no `data.device`, so a canvas-device-id collector
// that only excludes placeholders and proposal ghosts throws reading
// `.device.id` on one. This pins the extracted, unit-testable filter logic
// directly: a mixed node list (real device, proposal ghost device,
// placeholder, network element) must neither throw nor include anything but
// the one real device's id.

function device(id: string): Device {
  return {
    id,
    name: `device-${id}`,
    topology_type: "PHYSICAL",
  } as unknown as Device;
}

function deviceNode(id: string, deviceId: string, isProposal = false): Node<CanvasNodeData> {
  const data: DeviceNodeData = {
    device: device(deviceId),
    label: `device-${deviceId}`,
    topologyType: "PHYSICAL",
    ...(isProposal ? { isProposal: true } : {}),
  };
  return { id, type: "deviceNode", position: { x: 0, y: 0 }, data };
}

function placeholderNode(id: string): Node<CanvasNodeData> {
  return {
    id,
    type: "dynamicPlaceholderNode",
    position: { x: 0, y: 0 },
    data: { templateId: "t-1", templateName: "Template", templateIcon: null, count: 1 },
  };
}

function elementNode(id: string): Node<CanvasNodeData> {
  const data: NetworkElementNodeData = {
    element: { id: `elem-${id}`, element_type: "vlan_segment", label: "VLAN", attrs: {} },
  };
  return { id, type: "networkElementNode", position: { x: 0, y: 0 }, data };
}

// A full inventory Device record, the shape the pre-fix editor persisted onto
// every device node verbatim (issue: field_data can carry clear-text device
// credentials). persistableDevice/persistableCanvasNodes are the client-side
// narrowing that now runs before a canvas is ever sent to the server.
function fullDevice(id: string): Device {
  return {
    id,
    name: `device-${id}`,
    template_id: "tmpl-1",
    template_name: "Generic Switch",
    template_icon: "icon.svg",
    template_vendor: "Acme",
    template_model: "X1000",
    template_part_number: "PN-1",
    topology_type: "PHYSICAL",
    status: "AVAILABLE",
    field_data: { password: "super-secret", host: "10.0.0.1" },
    exclusive: true,
    driver_id: "driver-1",
    driver_name: "mock_l1",
    connection_type: "Layer 3 Switch",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    created_by: null,
    created_by_name: null,
    modified_by: null,
    modified_by_name: null,
    poll_interval_seconds: null,
    resolved_poll_interval_seconds: null,
  };
}

describe("persistableDevice", () => {
  it("drops field_data and every other non-allowlisted key", () => {
    const result = persistableDevice(fullDevice("dev-1"));
    expect(result).not.toHaveProperty("field_data");
    expect(result).not.toHaveProperty("template_id");
    expect(result).not.toHaveProperty("template_vendor");
    expect(result).not.toHaveProperty("template_model");
    expect(result).not.toHaveProperty("driver_id");
    expect(result).not.toHaveProperty("exclusive");
    expect(result).not.toHaveProperty("created_at");
  });

  it("keeps the allowlisted keys byte-for-byte", () => {
    const device = fullDevice("dev-1");
    const result = persistableDevice(device);
    expect(result).toEqual({
      id: "dev-1",
      name: "device-dev-1",
      topology_type: "PHYSICAL",
      connection_type: "Layer 3 Switch",
      status: "AVAILABLE",
      template_name: "Generic Switch",
      template_icon: "icon.svg",
    });
  });

  it("does not mutate the input device", () => {
    const device = fullDevice("dev-1");
    const before = { ...device };
    persistableDevice(device);
    expect(device).toEqual(before);
  });
});

describe("persistableCanvasNodes", () => {
  it("strips field_data from every device node and leaves other node types alone", () => {
    const nodes = [
      deviceNode("n1", "dev-1"),
      placeholderNode("n2"),
      elementNode("n3"),
    ];
    // deviceNode() above builds a thin Device via device(); swap in a full
    // one carrying field_data for this test.
    (nodes[0].data as DeviceNodeData).device = fullDevice("dev-1");

    const result = persistableCanvasNodes(nodes);

    const strippedDevice = (result[0].data as DeviceNodeData).device;
    expect(strippedDevice).not.toHaveProperty("field_data");
    expect(strippedDevice.id).toBe("dev-1");
    expect(result[1]).toEqual(nodes[1]);
    expect(result[2]).toEqual(nodes[2]);
  });

  it("a PUT payload built from a hydrated store node contains no field_data", () => {
    const node = deviceNode("n1", "dev-1");
    (node.data as DeviceNodeData).device = fullDevice("dev-1");
    const payload = { nodes: persistableCanvasNodes([node]), edges: [] };
    expect(JSON.stringify(payload)).not.toContain("field_data");
    expect(JSON.stringify(payload)).not.toContain("super-secret");
  });
});

describe("collectCanvasDeviceIds", () => {
  it("does not throw and returns only the real device's id for a mixed node list", () => {
    const nodes = [
      deviceNode("n1", "dev-1"),
      deviceNode("n2", "dev-2", /* isProposal */ true),
      placeholderNode("n3"),
      elementNode("n4"),
    ];

    let result: Set<string> | undefined;
    expect(() => {
      result = collectCanvasDeviceIds(nodes);
    }).not.toThrow();

    expect(result).toEqual(new Set(["dev-1"]));
  });

  it("returns an empty set for an all-element/placeholder canvas", () => {
    const nodes = [placeholderNode("n1"), elementNode("n2"), elementNode("n3")];
    expect(collectCanvasDeviceIds(nodes)).toEqual(new Set());
  });

  it("dedupes when two device nodes reference the same inventory device", () => {
    const nodes = [deviceNode("n1", "dev-1"), deviceNode("n2", "dev-1")];
    expect(collectCanvasDeviceIds(nodes)).toEqual(new Set(["dev-1"]));
  });
});
