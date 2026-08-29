import { describe, it, expect } from "vitest";
import type { Node } from "@xyflow/react";

import { collectCanvasDeviceIds } from "@/lib/canvasNodes";
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
