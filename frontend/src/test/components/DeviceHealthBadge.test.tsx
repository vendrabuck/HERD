import { render, screen } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";

const mockUseDeviceHealth = vi.fn();
vi.mock("@/api/health", () => ({
  useDeviceHealth: (...args: unknown[]) => mockUseDeviceHealth(...args),
}));

import { DeviceHealthBadge } from "@/components/inventory/DeviceHealthBadge";

describe("DeviceHealthBadge", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders Healthy in green when status is HEALTHY", () => {
    mockUseDeviceHealth.mockReturnValue({
      data: {
        device_id: "d1",
        last_polled_at: "2026-05-26T10:00:00Z",
        last_status: "HEALTHY",
        last_run_id: "run-1",
        consecutive_failures: 0,
        next_poll_at: null,
      },
      isLoading: false,
    });
    render(<DeviceHealthBadge deviceId="d1" />);
    const badge = screen.getByText("Healthy");
    expect(badge.className).toContain("bg-green-100");
  });

  it("renders Degraded in yellow", () => {
    mockUseDeviceHealth.mockReturnValue({
      data: {
        device_id: "d1",
        last_polled_at: "2026-05-26T10:00:00Z",
        last_status: "DEGRADED",
        last_run_id: null,
        consecutive_failures: 1,
        next_poll_at: null,
      },
      isLoading: false,
    });
    render(<DeviceHealthBadge deviceId="d1" />);
    expect(screen.getByText("Degraded").className).toContain("bg-yellow-100");
  });

  it("renders Unreachable in red", () => {
    mockUseDeviceHealth.mockReturnValue({
      data: {
        device_id: "d1",
        last_polled_at: "2026-05-26T10:00:00Z",
        last_status: "UNREACHABLE",
        last_run_id: null,
        consecutive_failures: 5,
        next_poll_at: null,
      },
      isLoading: false,
    });
    render(<DeviceHealthBadge deviceId="d1" />);
    expect(screen.getByText("Unreachable").className).toContain("bg-red-100");
  });

  it("renders Unknown in gray when device hasn't been polled", () => {
    mockUseDeviceHealth.mockReturnValue({
      data: {
        device_id: "d1",
        last_polled_at: null,
        last_status: "UNKNOWN",
        last_run_id: null,
        consecutive_failures: 0,
        next_poll_at: null,
      },
      isLoading: false,
    });
    render(<DeviceHealthBadge deviceId="d1" />);
    expect(screen.getByText("Unknown").className).toContain("bg-gray-100");
  });

  it("renders a placeholder while loading", () => {
    mockUseDeviceHealth.mockReturnValue({ data: undefined, isLoading: true });
    render(<DeviceHealthBadge deviceId="d1" />);
    expect(screen.getByText("...")).toBeInTheDocument();
  });

  it("renders 'Never polled' tooltip when last_polled_at is null", () => {
    mockUseDeviceHealth.mockReturnValue({
      data: {
        device_id: "d1",
        last_polled_at: null,
        last_status: "UNKNOWN",
        last_run_id: null,
        consecutive_failures: 0,
        next_poll_at: null,
      },
      isLoading: false,
    });
    render(<DeviceHealthBadge deviceId="d1" />);
    expect(screen.getByText("Unknown").getAttribute("title")).toBe("Never polled");
  });
});
