import { render, screen, fireEvent, within } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import {
  SOURCE_DEVICE,
  EMPTY_CONNECTIONS,
  mockStandardPorts,
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

import { ElementAttachDialog } from "@/components/topology-editor/ElementAttachDialog";

const OTHER_DEVICE = "device-other";
const SOURCE_CONNS = [connectionFixture("c1", SOURCE_DEVICE, "eth2", OTHER_DEVICE, "x1")];

function baseProps(overrides: Partial<React.ComponentProps<typeof ElementAttachDialog>> = {}) {
  return {
    open: true,
    deviceId: SOURCE_DEVICE,
    deviceName: "dut-01",
    deviceTopologyType: "PHYSICAL" as const,
    elementLabel: "Mgmt VLAN",
    elementType: "vlan_segment" as const,
    onConfirm: vi.fn(),
    onCancel: vi.fn(),
    ...overrides,
  };
}

function rowFor(text: string): HTMLElement {
  return screen.getByText(text).closest("[data-port-id]") as HTMLElement;
}

// ElementAttachDialog toggles selection directly on mousedown (PortColumn's
// row wires onPortMouseDown there; onPortActivate itself only fires on
// keyboard Enter/Space, see WiringDialog's own window-level press tracking
// for the click/drag arbitration this dialog does not need).
function clickPort(text: string) {
  fireEvent.mouseDown(rowFor(text), { clientX: 0, clientY: 0 });
}

describe("ElementAttachDialog", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockStandardPorts(mockUsePorts);
    mockUseDeviceConnections.mockImplementation((deviceId: string) => {
      if (deviceId === SOURCE_DEVICE) return { data: SOURCE_CONNS, isLoading: false };
      return { data: EMPTY_CONNECTIONS, isLoading: false };
    });
  });

  it("renders the device's port column and the element as a static target card, not a port list", () => {
    render(<ElementAttachDialog {...baseProps()} />);
    expect(screen.getByText("eth1")).toBeInTheDocument();
    expect(screen.getByText("eth2")).toBeInTheDocument();
    expect(screen.getByText("eth3")).toBeInTheDocument();
    // The element side shows its label as a card, never a second PortColumn.
    expect(screen.getByText("Mgmt VLAN")).toBeInTheDocument();
    expect(screen.queryByTestId("port-column-target")).not.toBeInTheDocument();
  });

  it("filters the device's ports via the search box (shared filterPorts)", () => {
    render(<ElementAttachDialog {...baseProps()} />);
    fireEvent.change(screen.getByLabelText("Filter source ports"), { target: { value: "eth2" } });
    expect(screen.getByText("eth2")).toBeInTheDocument();
    expect(screen.queryByText("eth1")).not.toBeInTheDocument();
  });

  it("a cabled port carries the informational CABLED tag but is still selectable", () => {
    render(<ElementAttachDialog {...baseProps()} />);
    const eth2Row = screen.getByTestId("port-row-sp2");
    expect(within(eth2Row).getByText("CABLED")).toBeInTheDocument();
    expect(eth2Row.getAttribute("data-status")).toBe("free");
  });

  it("a port already wired on the canvas (existingWiredPortIds) is unavailable", () => {
    render(<ElementAttachDialog {...baseProps({ existingWiredPortIds: new Set(["sp1"]) })} />);
    const eth1Row = screen.getByTestId("port-row-sp1");
    expect(eth1Row.getAttribute("data-status")).toBe("canvas-wired");
    expect(within(eth1Row).getByText("WIRED")).toBeInTheDocument();
  });

  it("multi-select: clicking multiple ports selects all of them (not an arm-then-pair single selection)", () => {
    render(<ElementAttachDialog {...baseProps()} />);
    clickPort("eth1");
    clickPort("eth2");
    clickPort("eth3");

    expect(screen.getByTestId("port-row-sp1").getAttribute("data-status")).toBe("session-wired");
    expect(screen.getByTestId("port-row-sp2").getAttribute("data-status")).toBe("session-wired");
    expect(screen.getByTestId("port-row-sp3").getAttribute("data-status")).toBe("session-wired");
    expect(screen.getByText("3 ports selected")).toBeInTheDocument();
  });

  it("clicking a selected port again deselects it", () => {
    render(<ElementAttachDialog {...baseProps()} />);
    clickPort("eth1");
    expect(screen.getByTestId("port-row-sp1").getAttribute("data-status")).toBe("session-wired");
    clickPort("eth1");
    expect(screen.getByTestId("port-row-sp1").getAttribute("data-status")).toBe("free");
  });

  it("Confirm is disabled with zero ports selected and enabled once one is", () => {
    render(<ElementAttachDialog {...baseProps()} />);
    expect(screen.getByRole("button", { name: "Attach" })).toBeDisabled();
    clickPort("eth1");
    expect(screen.getByRole("button", { name: "Attach 1 port" })).toBeEnabled();
  });

  it("Confirm emits every selected port as one attach selection in a single onConfirm call", () => {
    const onConfirm = vi.fn();
    render(<ElementAttachDialog {...baseProps({ onConfirm })} />);
    clickPort("eth1");
    clickPort("eth3");
    fireEvent.click(screen.getByRole("button", { name: "Attach 2 ports" }));

    expect(onConfirm).toHaveBeenCalledTimes(1);
    const selections = onConfirm.mock.calls[0][0];
    expect(selections).toHaveLength(2);
    expect(selections).toEqual(
      expect.arrayContaining([
        { portId: "sp1", portName: "eth1" },
        { portId: "sp3", portName: "eth3" },
      ]),
    );
  });

  it("Cancel calls onCancel without confirming", () => {
    const onConfirm = vi.fn();
    const onCancel = vi.fn();
    render(<ElementAttachDialog {...baseProps({ onConfirm, onCancel })} />);
    clickPort("eth1");
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("shows the element's icon-per-type card for each of the four element types without throwing", () => {
    for (const elementType of ["vlan_segment", "subnet", "external_cloud", "patch_trunk"] as const) {
      const { unmount } = render(<ElementAttachDialog {...baseProps({ elementType })} />);
      expect(screen.getByText("Mgmt VLAN")).toBeInTheDocument();
      unmount();
    }
  });
});
