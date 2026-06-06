import { render, screen } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";

const mockUseDeviceGroupsForDevice = vi.fn();
vi.mock("@/api/deviceGroups", () => ({
  useDeviceGroupsForDevice: (...args: unknown[]) => mockUseDeviceGroupsForDevice(...args),
}));

vi.mock("@/api/health", () => ({
  useDeviceHealth: () => ({
    data: {
      device_id: "device-1",
      last_polled_at: null,
      last_status: "UNKNOWN",
      last_run_id: null,
      consecutive_failures: 0,
      next_poll_at: null,
    },
    isLoading: false,
  }),
}));

import { DeviceInfoPanel } from "@/components/inventory/DeviceInfoPanel";
import type { Device } from "@/types/device.types";

const BASE_DEVICE: Device = {
  id: "device-1",
  name: "ex-01",
  template_id: "tmpl-1",
  template_name: "EX2200",
  topology_type: "PHYSICAL",
  status: "AVAILABLE",
  field_data: {},
  exclusive: true,
  created_at: "2026-01-15T00:00:00Z",
  updated_at: "2026-02-20T00:00:00Z",
  created_by: "user-1",
  created_by_name: "alice",
  modified_by: "user-2",
  modified_by_name: "bob",
} as Device;

describe("DeviceInfoPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders audit names", () => {
    mockUseDeviceGroupsForDevice.mockReturnValue({ data: [], isLoading: false });
    render(<DeviceInfoPanel device={BASE_DEVICE} />);
    expect(screen.getByText("alice")).toBeInTheDocument();
    expect(screen.getByText("bob")).toBeInTheDocument();
    expect(screen.getByText("Created by:")).toBeInTheDocument();
    expect(screen.getByText("Modified by:")).toBeInTheDocument();
  });

  it("shows 'Unknown' when audit names are missing", () => {
    mockUseDeviceGroupsForDevice.mockReturnValue({ data: [], isLoading: false });
    render(
      <DeviceInfoPanel
        device={{ ...BASE_DEVICE, created_by_name: null, modified_by_name: null } as Device}
      />
    );
    // Scope to <dd> elements: audit fallback rendering uses a description list,
    // so the health-badge "Unknown" (a <span>) does not match.
    const auditUnknown = screen
      .getAllByText("Unknown")
      .filter((el) => el.tagName === "DD");
    expect(auditUnknown).toHaveLength(2);
  });

  it("renders dates section with Created and Modified labels", () => {
    mockUseDeviceGroupsForDevice.mockReturnValue({ data: [], isLoading: false });
    render(<DeviceInfoPanel device={BASE_DEVICE} />);
    expect(screen.getByText("Dates")).toBeInTheDocument();
    expect(screen.getByText("Created:")).toBeInTheDocument();
    expect(screen.getByText("Modified:")).toBeInTheDocument();
  });

  it("shows loading state for device groups", () => {
    mockUseDeviceGroupsForDevice.mockReturnValue({ data: undefined, isLoading: true });
    render(<DeviceInfoPanel device={BASE_DEVICE} />);
    expect(screen.getByText("Loading...")).toBeInTheDocument();
  });

  it("shows 'None' when device has no group memberships", () => {
    mockUseDeviceGroupsForDevice.mockReturnValue({ data: [], isLoading: false });
    render(<DeviceInfoPanel device={BASE_DEVICE} />);
    expect(screen.getByText("None")).toBeInTheDocument();
  });

  it("renders group memberships with nested user group names", () => {
    mockUseDeviceGroupsForDevice.mockReturnValue({
      data: [
        {
          id: "dg-1",
          name: "Santa Clara Lab",
          user_groups: [
            { user_group_id: "ug-1", user_group_name: "SCLAB Tier 1" },
            { user_group_id: "ug-2", user_group_name: "Lab Admins" },
          ],
        },
      ],
      isLoading: false,
    });
    render(<DeviceInfoPanel device={BASE_DEVICE} />);
    expect(screen.getByText("Santa Clara Lab")).toBeInTheDocument();
    expect(screen.getByText("SCLAB Tier 1")).toBeInTheDocument();
    expect(screen.getByText("Lab Admins")).toBeInTheDocument();
  });

  it("falls back to a truncated id when user_group_name is missing", () => {
    mockUseDeviceGroupsForDevice.mockReturnValue({
      data: [
        {
          id: "dg-1",
          name: "Plano Lab",
          user_groups: [
            { user_group_id: "abcdef0123456789", user_group_name: null },
          ],
        },
      ],
      isLoading: false,
    });
    render(<DeviceInfoPanel device={BASE_DEVICE} />);
    expect(screen.getByText("abcdef01...")).toBeInTheDocument();
  });

  it("shows hardware identity chip when template vendor and model are set", () => {
    mockUseDeviceGroupsForDevice.mockReturnValue({ data: [], isLoading: false });
    render(
      <DeviceInfoPanel
        device={{
          ...BASE_DEVICE,
          template_vendor: "Cisco",
          template_model: "Catalyst 9300",
        } as Device}
      />,
    );
    expect(screen.getByText("Hardware")).toBeInTheDocument();
    expect(screen.getByText("Cisco, Catalyst 9300")).toBeInTheDocument();
  });

  it("hides hardware identity chip when vendor is 'unknown'", () => {
    mockUseDeviceGroupsForDevice.mockReturnValue({ data: [], isLoading: false });
    render(
      <DeviceInfoPanel
        device={{
          ...BASE_DEVICE,
          template_vendor: "unknown",
          template_model: "unknown",
        } as Device}
      />,
    );
    expect(screen.queryByText("Hardware")).not.toBeInTheDocument();
  });

  it("hides hardware identity chip when vendor and model are null", () => {
    mockUseDeviceGroupsForDevice.mockReturnValue({ data: [], isLoading: false });
    render(
      <DeviceInfoPanel
        device={{
          ...BASE_DEVICE,
          template_vendor: null,
          template_model: null,
        } as Device}
      />,
    );
    expect(screen.queryByText("Hardware")).not.toBeInTheDocument();
  });
});
