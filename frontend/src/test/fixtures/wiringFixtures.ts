import type { Mock } from "vitest";
import type { Port } from "@/types/port.types";
import type { Connection } from "@/types/connection.types";

// Shared port/device fixtures for the wiring dialog test suite (issue #517
// review item 11): WiringDialog.test.tsx, QuickConnectPopover.test.tsx, and
// TopologyEditorWiring.test.tsx all wired two seeded-looking devices with
// the same shape; this is the one place that shape is defined. vi.mock(...)
// calls themselves stay in each test file (vitest hoists them above imports,
// so the factory body has to be either fully inline or close only over
// vi.hoisted() state declared in that same file to be safe); these helpers
// just remove the duplicated fixture DATA and default-wiring boilerplate.

export const SOURCE_DEVICE = "device-src";
export const TARGET_DEVICE = "device-tgt";
export const OTHER_DEVICE = "device-other";

function port(id: string, deviceId: string, name: string): Port {
  return {
    id,
    device_id: deviceId,
    name,
    template_id: "t",
    template_name: null,
    template_icon: null,
    field_data: {},
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  };
}

export const SOURCE_PORTS: Port[] = [
  port("sp1", SOURCE_DEVICE, "eth1"),
  port("sp2", SOURCE_DEVICE, "eth2"),
  port("sp3", SOURCE_DEVICE, "eth3"),
];
export const TARGET_PORTS: Port[] = [
  port("tp1", TARGET_DEVICE, "0/0/1"),
  port("tp2", TARGET_DEVICE, "0/0/2"),
  port("tp3", TARGET_DEVICE, "0/0/3"),
];

// Stable references reused across every mock call (issue #517 review item
// 10b): a fresh `[]` literal returned per call defeats usePortAvailability's
// memoization the same way an actually-buggy query client would; real
// TanStack Query returns the same reference for unchanged data via
// structural sharing, so the mocks below match that instead of
// (accidentally) simulating the bug they were written to catch.
export const EMPTY_PORTS: Port[] = [];
export const EMPTY_CONNECTIONS: Connection[] = [];

export function mockStandardPorts(mockUsePorts: Mock) {
  mockUsePorts.mockImplementation((deviceId: string) => {
    if (deviceId === SOURCE_DEVICE) return { data: SOURCE_PORTS, isLoading: false };
    if (deviceId === TARGET_DEVICE) return { data: TARGET_PORTS, isLoading: false };
    return { data: EMPTY_PORTS, isLoading: false };
  });
}

export function mockNoConnections(mockUseDeviceConnections: Mock) {
  mockUseDeviceConnections.mockReturnValue({ data: EMPTY_CONNECTIONS, isLoading: false });
}

// A connection row cabling `portName` on `deviceId` to `otherPortName` on
// `otherDeviceId` (the shape the cabling service returns).
export function connectionFixture(
  id: string,
  deviceId: string,
  portName: string,
  otherDeviceId: string,
  otherPortName: string,
): Connection {
  return {
    id,
    device_a_id: deviceId,
    port_a: portName,
    device_b_id: otherDeviceId,
    port_b: otherPortName,
    connection_type: "L1",
    notes: null,
    created_by: "u",
    created_at: "2026-01-01T00:00:00Z",
    modified_by: null,
    updated_at: null,
  };
}
