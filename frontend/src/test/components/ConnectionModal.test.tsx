import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi, beforeAll, beforeEach } from "vitest";

beforeAll(() => {
  HTMLDialogElement.prototype.showModal = vi.fn();
  HTMLDialogElement.prototype.close = vi.fn();
});

const mockUsePorts = vi.fn();
const mockUseDeviceConnections = vi.fn();

vi.mock("@/api/ports", () => ({
  usePorts: (...args: unknown[]) => mockUsePorts(...args),
}));
vi.mock("@/api/connections", () => ({
  useDeviceConnections: (...args: unknown[]) => mockUseDeviceConnections(...args),
}));

import { ConnectionModal } from "@/components/topology-editor/ConnectionModal";

const SOURCE_DEVICE = "device-src";
const TARGET_DEVICE = "device-tgt";

const SOURCE_PORTS = [
  { id: "sp1", device_id: SOURCE_DEVICE, name: "eth1", template_id: "t", field_data: {} },
  { id: "sp2", device_id: SOURCE_DEVICE, name: "eth2", template_id: "t", field_data: {} },
];
const TARGET_PORTS = [
  { id: "tp1", device_id: TARGET_DEVICE, name: "0/0/1", template_id: "t", field_data: {} },
  { id: "tp2", device_id: TARGET_DEVICE, name: "0/0/2", template_id: "t", field_data: {} },
];

function baseProps() {
  return {
    open: true,
    sourceDeviceId: SOURCE_DEVICE,
    sourceDeviceName: "dut-01",
    targetDeviceId: TARGET_DEVICE,
    targetDeviceName: "switch-01",
    defaultLayer: "L1" as const,
    onConfirm: vi.fn(),
    onCancel: vi.fn(),
  };
}

describe("ConnectionModal - uncabled port UX", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockUsePorts.mockImplementation((deviceId: string) => {
      if (deviceId === SOURCE_DEVICE) return { data: SOURCE_PORTS, isLoading: false };
      if (deviceId === TARGET_DEVICE) return { data: TARGET_PORTS, isLoading: false };
      return { data: [], isLoading: false };
    });
    mockUseDeviceConnections.mockReturnValue({ data: [], isLoading: false });
  });

  it("appends '(no cable)' to ports with no physical connection", () => {
    mockUseDeviceConnections.mockReturnValue({ data: [], isLoading: false });
    render(<ConnectionModal {...baseProps()} />);
    // Both source ports have no connections, so both marked as (no cable).
    const options = screen.getAllByRole("option", { hidden: true });
    const labels = options.map((o) => o.textContent);
    expect(labels).toContain("eth1 (no cable)");
    expect(labels).toContain("eth2 (no cable)");
    expect(labels).toContain("0/0/1 (no cable)");
    expect(labels).toContain("0/0/2 (no cable)");
  });

  it("omits '(no cable)' for ports with an existing physical connection", () => {
    mockUseDeviceConnections.mockImplementation((deviceId: string) => {
      if (deviceId === SOURCE_DEVICE) {
        return {
          data: [
            {
              id: "c1",
              device_a_id: SOURCE_DEVICE,
              port_a: "eth1",
              device_b_id: "other",
              port_b: "eth9",
              connection_type: "L1",
            },
          ],
          isLoading: false,
        };
      }
      return { data: [], isLoading: false };
    });
    render(<ConnectionModal {...baseProps()} />);
    const labels = screen.getAllByRole("option", { hidden: true }).map((o) => o.textContent);
    expect(labels).toContain("eth1"); // cabled, no suffix
    expect(labels).toContain("eth2 (no cable)");
  });

  it("shows warning text when a selected source port is uncabled", () => {
    render(<ConnectionModal {...baseProps()} />);
    const [sourceSelect] = screen.getAllByRole("combobox", { hidden: true });
    fireEvent.change(sourceSelect, { target: { value: "sp1" } });
    expect(
      screen.getByText("This port has no physical cable connected")
    ).toBeInTheDocument();
  });

  it("sets portsCabled=false on the edge data when either port is uncabled", () => {
    // Source has a cable; target does not.
    mockUseDeviceConnections.mockImplementation((deviceId: string) => {
      if (deviceId === SOURCE_DEVICE) {
        return {
          data: [
            {
              id: "c1",
              device_a_id: SOURCE_DEVICE,
              port_a: "eth1",
              device_b_id: "other",
              port_b: "eth9",
              connection_type: "L1",
            },
          ],
          isLoading: false,
        };
      }
      return { data: [], isLoading: false };
    });
    const props = baseProps();
    render(<ConnectionModal {...props} />);
    const [sourceSelect, targetSelect] = screen.getAllByRole("combobox", { hidden: true });
    fireEvent.change(sourceSelect, { target: { value: "sp1" } }); // cabled
    fireEvent.change(targetSelect, { target: { value: "tp1" } }); // uncabled

    fireEvent.click(screen.getByText("Connect", { selector: "button" }));
    expect(props.onConfirm).toHaveBeenCalledWith(
      expect.objectContaining({ portsCabled: false })
    );
  });

  it("sets portsCabled=true when both ports have cables", () => {
    mockUseDeviceConnections.mockImplementation((deviceId: string) => {
      if (deviceId === SOURCE_DEVICE) {
        return {
          data: [
            {
              id: "c1",
              device_a_id: SOURCE_DEVICE,
              port_a: "eth1",
              device_b_id: "other",
              port_b: "eth9",
              connection_type: "L1",
            },
          ],
          isLoading: false,
        };
      }
      if (deviceId === TARGET_DEVICE) {
        return {
          data: [
            {
              id: "c2",
              device_a_id: "other2",
              port_a: "eth7",
              device_b_id: TARGET_DEVICE,
              port_b: "0/0/1",
              connection_type: "L1",
            },
          ],
          isLoading: false,
        };
      }
      return { data: [], isLoading: false };
    });
    const props = baseProps();
    render(<ConnectionModal {...props} />);
    const [sourceSelect, targetSelect] = screen.getAllByRole("combobox", { hidden: true });
    fireEvent.change(sourceSelect, { target: { value: "sp1" } });
    fireEvent.change(targetSelect, { target: { value: "tp1" } });
    fireEvent.click(screen.getByText("Connect", { selector: "button" }));
    expect(props.onConfirm).toHaveBeenCalledWith(
      expect.objectContaining({ portsCabled: true })
    );
  });

  it("disables Connect until both ports are selected", () => {
    render(<ConnectionModal {...baseProps()} />);
    const connect = screen.getByText("Connect", { selector: "button" }) as HTMLButtonElement;
    expect(connect.disabled).toBe(true);
    const [sourceSelect, targetSelect] = screen.getAllByRole("combobox", { hidden: true });
    fireEvent.change(sourceSelect, { target: { value: "sp1" } });
    expect(connect.disabled).toBe(true);
    fireEvent.change(targetSelect, { target: { value: "tp1" } });
    expect(connect.disabled).toBe(false);
  });
});
