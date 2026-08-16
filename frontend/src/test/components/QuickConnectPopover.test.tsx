import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import {
  SOURCE_DEVICE,
  TARGET_DEVICE,
  OTHER_DEVICE,
  TARGET_PORTS,
  EMPTY_PORTS,
  mockStandardPorts,
  mockNoConnections,
  connectionFixture,
} from "../fixtures/wiringFixtures";

const mockUsePorts = vi.fn();
const mockUseDeviceConnections = vi.fn();

vi.mock("@/api/ports", () => ({
  usePorts: (...args: unknown[]) => mockUsePorts(...args),
}));
vi.mock("@/api/connections", () => ({
  useDeviceConnections: (...args: unknown[]) => mockUseDeviceConnections(...args),
}));

import { QuickConnectPopover } from "@/components/topology-editor/QuickConnectPopover";

function baseProps(overrides: Partial<React.ComponentProps<typeof QuickConnectPopover>> = {}) {
  return {
    open: true,
    sourceDeviceId: SOURCE_DEVICE,
    sourceDeviceName: "dut-01",
    targetDeviceId: TARGET_DEVICE,
    targetDeviceName: "switch-01",
    defaultLayer: "L1" as const,
    onConfirm: vi.fn(),
    onCancel: vi.fn(),
    onEscalate: vi.fn(),
    ...overrides,
  };
}

// Restores the OLD ConnectionModal semantics (issue #517 review item 1): a
// port with ANY registered physical connection, cabled to any device at
// all, is selectable and carries no suffix; an uncabled port is selectable
// too but flagged "(no cable)". Nothing is disabled by cabling state.
describe("QuickConnectPopover - cabling semantics", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockStandardPorts(mockUsePorts);
    mockNoConnections(mockUseDeviceConnections);
  });

  it("appends '(no cable)' to ports with no physical connection", () => {
    render(<QuickConnectPopover {...baseProps()} />);
    const labels = screen.getAllByRole("option", { hidden: true }).map((o) => o.textContent);
    expect(labels).toContain("eth1 (no cable)");
    expect(labels).toContain("eth2 (no cable)");
    expect(labels).toContain("0/0/1 (no cable)");
    expect(labels).toContain("0/0/2 (no cable)");
  });

  it("omits '(no cable)' for a port with an existing physical connection, to ANY device (HERD fabric shape: DUT through an L1 switch, not directly to the counterpart)", () => {
    mockUseDeviceConnections.mockImplementation((deviceId: string) => {
      if (deviceId === SOURCE_DEVICE) {
        return { data: [connectionFixture("c1", SOURCE_DEVICE, "eth1", OTHER_DEVICE, "eth9")], isLoading: false };
      }
      return { data: [], isLoading: false };
    });
    render(<QuickConnectPopover {...baseProps()} />);
    const labels = screen.getAllByRole("option", { hidden: true }).map((o) => o.textContent);
    expect(labels).toContain("eth1"); // cabled (to a third device), no suffix
    expect(labels).toContain("eth2 (no cable)");
  });

  it("no option is ever disabled by cabling state alone", () => {
    mockUseDeviceConnections.mockImplementation((deviceId: string) => {
      if (deviceId === SOURCE_DEVICE) {
        return { data: [connectionFixture("c1", SOURCE_DEVICE, "eth1", OTHER_DEVICE, "x1")], isLoading: false };
      }
      return { data: [], isLoading: false };
    });
    render(<QuickConnectPopover {...baseProps()} />);
    const options = screen.getAllByRole("option", { hidden: true }) as HTMLOptionElement[];
    expect(options.every((o) => !o.disabled)).toBe(true);
  });

  it("shows warning text when a selected source port is uncabled", () => {
    render(<QuickConnectPopover {...baseProps()} />);
    const [sourceSelect] = screen.getAllByRole("combobox", { hidden: true });
    fireEvent.change(sourceSelect, { target: { value: "sp1" } });
    expect(screen.getByText("This port has no physical cable connected")).toBeInTheDocument();
  });

  it("sets portsCabled=false on the edge data when either port is uncabled", () => {
    mockUseDeviceConnections.mockImplementation((deviceId: string) => {
      if (deviceId === SOURCE_DEVICE) {
        return { data: [connectionFixture("c1", SOURCE_DEVICE, "eth1", OTHER_DEVICE, "eth9")], isLoading: false };
      }
      return { data: [], isLoading: false };
    });
    const props = baseProps();
    render(<QuickConnectPopover {...props} />);
    const [sourceSelect, targetSelect] = screen.getAllByRole("combobox", { hidden: true });
    fireEvent.change(sourceSelect, { target: { value: "sp1" } }); // cabled
    fireEvent.change(targetSelect, { target: { value: "tp1" } }); // uncabled

    fireEvent.click(screen.getByText("Connect", { selector: "button" }));
    expect(props.onConfirm).toHaveBeenCalledWith(expect.objectContaining({ portsCabled: false }));
  });

  it("sets portsCabled=true when both ports have cables, restoring the intent of the ported-forward ConnectionModal test", () => {
    mockUseDeviceConnections.mockImplementation((deviceId: string) => {
      if (deviceId === SOURCE_DEVICE) {
        return { data: [connectionFixture("c1", SOURCE_DEVICE, "eth1", OTHER_DEVICE, "eth9")], isLoading: false };
      }
      if (deviceId === TARGET_DEVICE) {
        return { data: [connectionFixture("c2", OTHER_DEVICE, "eth7", TARGET_DEVICE, "0/0/1")], isLoading: false };
      }
      return { data: [], isLoading: false };
    });
    const props = baseProps();
    render(<QuickConnectPopover {...props} />);
    const [sourceSelect, targetSelect] = screen.getAllByRole("combobox", { hidden: true });
    fireEvent.change(sourceSelect, { target: { value: "sp1" } });
    fireEvent.change(targetSelect, { target: { value: "tp1" } });
    fireEvent.click(screen.getByText("Connect", { selector: "button" }));
    expect(props.onConfirm).toHaveBeenCalledWith(expect.objectContaining({ portsCabled: true }));
  });

  it("disables Connect until both ports are selected", () => {
    render(<QuickConnectPopover {...baseProps()} />);
    const connect = screen.getByText("Connect", { selector: "button" }) as HTMLButtonElement;
    expect(connect.disabled).toBe(true);
    const [sourceSelect, targetSelect] = screen.getAllByRole("combobox", { hidden: true });
    fireEvent.change(sourceSelect, { target: { value: "sp1" } });
    expect(connect.disabled).toBe(true);
    fireEvent.change(targetSelect, { target: { value: "tp1" } });
    expect(connect.disabled).toBe(false);
  });

  it("while connections are loading, both selects are disabled and Connect stays disabled", () => {
    mockUseDeviceConnections.mockImplementation((deviceId: string) => {
      if (deviceId === SOURCE_DEVICE) return { data: [], isLoading: true };
      return { data: [], isLoading: false };
    });
    render(<QuickConnectPopover {...baseProps()} />);
    const [sourceSelect, targetSelect] = screen.getAllByRole("combobox", { hidden: true });
    expect(sourceSelect).toBeDisabled();
    expect(targetSelect).toBeDisabled();
    fireEvent.change(sourceSelect, { target: { value: "sp1" } });
    fireEvent.change(targetSelect, { target: { value: "tp1" } });
    expect(screen.getByRole("button", { name: "Connect" })).toBeDisabled();
  });

  it("confirming emits the picked ports, names, and layer", () => {
    const onConfirm = vi.fn();
    render(<QuickConnectPopover {...baseProps({ onConfirm })} />);
    const [sourceSelect, targetSelect] = screen.getAllByRole("combobox", { hidden: true });
    fireEvent.change(sourceSelect, { target: { value: "sp1" } });
    fireEvent.change(targetSelect, { target: { value: "tp1" } });
    fireEvent.click(screen.getByRole("button", { name: "L3" }));
    fireEvent.click(screen.getByRole("button", { name: "Connect" }));

    expect(onConfirm).toHaveBeenCalledWith(
      expect.objectContaining({
        source_port_id: "sp1",
        source_port_name: "eth1",
        target_port_id: "tp1",
        target_port_name: "0/0/1",
        layer: "L3",
      }),
    );
  });

  it("Open wiring dialog invokes the escalation callback", () => {
    const onEscalate = vi.fn();
    render(<QuickConnectPopover {...baseProps({ onEscalate })} />);
    fireEvent.click(screen.getByRole("button", { name: "Open wiring dialog" }));
    expect(onEscalate).toHaveBeenCalled();
  });

  it("a port already wired to the counterpart by an existing canvas edge is disabled with '(already connected)' (review item 8)", () => {
    render(
      <QuickConnectPopover
        {...baseProps({
          existingWiredSourcePortIds: new Set(["sp1"]),
          existingWiredTargetPortIds: new Set(["tp1"]),
        })}
      />,
    );
    const options = screen.getAllByRole("option", { hidden: true }) as HTMLOptionElement[];
    const sourceDisabled = options.find((o) => o.value === "sp1")!;
    expect(sourceDisabled.disabled).toBe(true);
    expect(sourceDisabled.textContent).toBe("eth1 (already connected)");
    const targetDisabled = options.find((o) => o.value === "tp1")!;
    expect(targetDisabled.disabled).toBe(true);
    expect(targetDisabled.textContent).toBe("0/0/1 (already connected)");

    const freeOption = options.find((o) => o.value === "sp2")!;
    expect(freeOption.disabled).toBe(false);
  });

  it("a device with no ports shows the disabled empty-state select (review item 9)", () => {
    mockUsePorts.mockImplementation((deviceId: string) => {
      if (deviceId === SOURCE_DEVICE) return { data: EMPTY_PORTS, isLoading: false };
      if (deviceId === TARGET_DEVICE) return { data: TARGET_PORTS, isLoading: false };
      return { data: EMPTY_PORTS, isLoading: false };
    });
    render(<QuickConnectPopover {...baseProps()} />);
    expect(screen.getByText("No ports configured")).toBeInTheDocument();
    const selects = screen.getAllByRole("combobox", { hidden: true }) as HTMLSelectElement[];
    expect(selects[0]).toBeDisabled();
  });
});
