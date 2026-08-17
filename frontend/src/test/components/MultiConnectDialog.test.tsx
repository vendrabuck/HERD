import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import {
  SOURCE_DEVICE,
  TARGET_DEVICE,
  SOURCE_PORTS,
  TARGET_PORTS,
  EMPTY_PORTS,
  EMPTY_CONNECTIONS,
  mockStandardPorts,
  connectionFixture,
} from "../fixtures/wiringFixtures";
import type { BulkConnectionResult } from "@/types/connection.types";
import type { Port } from "@/types/port.types";

const mockUsePorts = vi.fn();
const mockUseDeviceConnections = vi.fn();
const mockUsePaginatedDevices = vi.fn();

vi.mock("@/api/ports", () => ({
  usePorts: (...args: unknown[]) => mockUsePorts(...args),
}));
vi.mock("@/api/connections", () => ({
  BULK_CONNECTION_LIMIT: 200,
  useDeviceConnections: (...args: unknown[]) => mockUseDeviceConnections(...args),
}));
vi.mock("@/api/inventory", () => ({
  usePaginatedDevices: (...args: unknown[]) => mockUsePaginatedDevices(...args),
}));

import { MultiConnectDialog } from "@/components/admin/connections/MultiConnectDialog";

const DEVICE_A_NAME = "spine-1";
const DEVICE_B_NAME = "leaf-2";

// Stable reference: a fresh object per mock call would churn every consumer
// memo the way a genuinely broken query client does.
const DEVICE_RESULTS = {
  data: {
    items: [
      { id: SOURCE_DEVICE, name: DEVICE_A_NAME, topology_type: "PHYSICAL" },
      { id: TARGET_DEVICE, name: DEVICE_B_NAME, topology_type: "PHYSICAL" },
    ],
  },
};

// Stable module-level slice, never a fresh literal inside a mock body: an
// array rebuilt per mock call is exactly the instability that loops the
// geometry effect.
const SOURCE_PORTS_MINUS_ETH1 = SOURCE_PORTS.slice(1);

const ALL_CREATED: BulkConnectionResult = {
  created: 1,
  rejected: 0,
  rows: [{ index: 0, status: "created", connection_id: "c1", error: null }],
};

function createdResult(n: number): BulkConnectionResult {
  return {
    created: n,
    rejected: 0,
    rows: Array.from({ length: n }, (_, index) => ({
      index,
      status: "created" as const,
      connection_id: `c${index}`,
      error: null,
    })),
  };
}

function baseProps(overrides: Partial<React.ComponentProps<typeof MultiConnectDialog>> = {}) {
  return {
    open: true,
    onSubmit: vi.fn().mockResolvedValue(ALL_CREATED),
    onSuccess: vi.fn(),
    onCancel: vi.fn(),
    ...overrides,
  };
}

function rowFor(text: string): HTMLElement {
  return screen.getByText(text).closest("[data-port-id]") as HTMLElement;
}

// The full mousedown/mouseup/click sequence a real plain click dispatches, with
// no intervening mousemove: the only path the dialog treats as a click.
function clickRow(row: HTMLElement) {
  fireEvent.mouseDown(row, { clientX: 0, clientY: 0 });
  fireEvent.mouseUp(row, { clientX: 0, clientY: 0 });
  fireEvent.click(row);
}

function clickPort(text: string) {
  clickRow(rowFor(text));
}

function connectViaClick(sourceText: string, targetText: string) {
  clickPort(sourceText);
  clickPort(targetText);
}

function dragPort(fromText: string, toText: string) {
  const fromRow = rowFor(fromText);
  const toRow = rowFor(toText);
  fireEvent.mouseDown(fromRow, { clientX: 0, clientY: 0 });
  fireEvent.mouseMove(window, { clientX: 50, clientY: 50 });
  fireEvent.mouseUp(toRow, { clientX: 50, clientY: 50 });
}

function pickDevice(label: string, name: string) {
  fireEvent.change(screen.getByLabelText(`Search ${label}`), { target: { value: "de" } });
  fireEvent.click(screen.getByRole("button", { name }));
}

function pickBothDevices(aName = DEVICE_A_NAME, bName = DEVICE_B_NAME) {
  pickDevice("Device A", aName);
  pickDevice("Device B", bName);
}

function ports(count: number, deviceId: string, prefix: string): Port[] {
  return Array.from({ length: count }, (_, i) => ({
    id: `${prefix}${i}`,
    device_id: deviceId,
    name: `${prefix}-${i}`,
    template_id: "t",
    template_name: null,
    template_icon: null,
    field_data: {},
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  }));
}

describe("MultiConnectDialog", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockStandardPorts(mockUsePorts);
    mockUseDeviceConnections.mockReturnValue({ data: EMPTY_CONNECTIONS, isLoading: false });
    mockUsePaginatedDevices.mockReturnValue(DEVICE_RESULTS);
  });

  it("before any device is picked, both columns are empty and nothing can be created", () => {
    render(<MultiConnectDialog {...baseProps()} />);
    expect(screen.getAllByText("No device selected")).toHaveLength(2);
    expect(screen.getByText("Pick a device on each side to list its ports")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create connections" })).toBeDisabled();
  });

  it("Connect 1:1 in order refuses before both devices are picked", () => {
    render(<MultiConnectDialog {...baseProps()} />);
    fireEvent.click(screen.getByRole("button", { name: "Connect 1:1 in order" }));
    expect(screen.getByText("Pick a device on each side first")).toBeInTheDocument();
  });

  it("picking both devices lists their ports in two columns", () => {
    render(<MultiConnectDialog {...baseProps()} />);
    pickBothDevices();
    expect(screen.getByText("eth1")).toBeInTheDocument();
    expect(screen.getByText("0/0/1")).toBeInTheDocument();
    expect(screen.queryByText("No device selected")).not.toBeInTheDocument();
  });

  it("click-to-connect stages a line and marks both ports used for the session", () => {
    render(<MultiConnectDialog {...baseProps()} />);
    pickBothDevices();

    clickPort("eth1");
    expect(
      screen.getByText("eth1 selected. Click a port on the other side to connect"),
    ).toBeInTheDocument();

    clickPort("0/0/1");
    expect(screen.getByRole("button", { name: "Create 1 connection" })).toBeEnabled();
    expect(within(screen.getByTestId("port-row-sp1")).getByText("WIRED")).toBeInTheDocument();
    expect(within(screen.getByTestId("port-row-tp1")).getByText("WIRED")).toBeInTheDocument();
  });

  it("drag (mousedown, move past the threshold, mouseup on the other column) stages a line", () => {
    render(<MultiConnectDialog {...baseProps()} />);
    pickBothDevices();
    dragPort("eth3", "0/0/2");
    expect(screen.getByRole("button", { name: "Create 1 connection" })).toBeEnabled();
  });

  it("the session-used hard block: a port already carrying a staged line refuses a second one", () => {
    render(<MultiConnectDialog {...baseProps()} />);
    pickBothDevices();
    connectViaClick("eth1", "0/0/1");

    clickPort("eth1");
    expect(screen.getByText("eth1 already has a line in this session")).toBeInTheDocument();
    // Still exactly one line: the refusal staged nothing.
    expect(screen.getByRole("button", { name: "Review (1)" })).toBeInTheDocument();
  });

  it("an already-cabled port is flagged CABLED but stays fully selectable (warn, never block)", async () => {
    mockUseDeviceConnections.mockImplementation((deviceId: string | undefined) => {
      if (deviceId === SOURCE_DEVICE) {
        return {
          data: [connectionFixture("c1", SOURCE_DEVICE, "eth1", "device-other", "x1")],
          isLoading: false,
        };
      }
      return { data: EMPTY_CONNECTIONS, isLoading: false };
    });
    const onSubmit = vi.fn().mockResolvedValue(ALL_CREATED);
    render(<MultiConnectDialog {...baseProps({ onSubmit })} />);
    pickBothDevices();

    const eth1Row = screen.getByTestId("port-row-sp1");
    expect(within(eth1Row).getByText("CABLED")).toBeInTheDocument();
    expect(eth1Row.getAttribute("data-status")).toBe("free");
    expect(eth1Row).toHaveAttribute("tabindex", "0");

    connectViaClick("eth1", "0/0/1");
    fireEvent.click(screen.getByRole("button", { name: "Create 1 connection" }));
    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
    expect(onSubmit.mock.calls[0][0]).toEqual([
      expect.objectContaining({ port_a: "eth1", port_b: "0/0/1" }),
    ]);
  });

  it("a pair duplicating an existing connection is flagged, and is still confirmable", async () => {
    mockUseDeviceConnections.mockImplementation((deviceId: string | undefined) => {
      if (deviceId === SOURCE_DEVICE) {
        return {
          data: [connectionFixture("c1", SOURCE_DEVICE, "eth1", TARGET_DEVICE, "0/0/1")],
          isLoading: false,
        };
      }
      return { data: EMPTY_CONNECTIONS, isLoading: false };
    });
    const onSubmit = vi.fn().mockResolvedValue(ALL_CREATED);
    const onSuccess = vi.fn();
    render(<MultiConnectDialog {...baseProps({ onSubmit, onSuccess })} />);
    pickBothDevices();

    connectViaClick("eth1", "0/0/1");
    expect(screen.getByTestId("duplicate-notice")).toHaveTextContent(
      "1 staged pair duplicates an existing connection",
    );
    fireEvent.click(screen.getByRole("button", { name: "Review (1)" }));
    expect(within(screen.getByTestId("review-strip")).getByText("duplicate")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Create 1 connection" }));
    await waitFor(() => expect(onSuccess).toHaveBeenCalledWith(1));
  });

  it("a pair that does not match an existing connection is NOT flagged as a duplicate", () => {
    mockUseDeviceConnections.mockImplementation((deviceId: string | undefined) => {
      if (deviceId === SOURCE_DEVICE) {
        return {
          data: [connectionFixture("c1", SOURCE_DEVICE, "eth1", TARGET_DEVICE, "0/0/1")],
          isLoading: false,
        };
      }
      return { data: EMPTY_CONNECTIONS, isLoading: false };
    });
    render(<MultiConnectDialog {...baseProps()} />);
    pickBothDevices();
    // Same source port, DIFFERENT target port: not the registered pair.
    connectViaClick("eth1", "0/0/2");
    expect(screen.queryByTestId("duplicate-notice")).not.toBeInTheDocument();
  });

  it("while connections are loading every path is inert: staging, 1:1, and confirm", () => {
    mockUseDeviceConnections.mockImplementation((deviceId: string | undefined) => {
      if (deviceId === SOURCE_DEVICE) return { data: EMPTY_CONNECTIONS, isLoading: true };
      return { data: EMPTY_CONNECTIONS, isLoading: false };
    });
    render(<MultiConnectDialog {...baseProps()} />);
    pickBothDevices();

    connectViaClick("eth1", "0/0/1");
    expect(screen.getByRole("button", { name: "Create connections" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Connect 1:1 in order" })).toBeDisabled();
    expect(screen.queryByText("WIRED")).not.toBeInTheDocument();
  });

  it("mounts without an infinite render loop when both queries are still undefined", () => {
    // The regression guard: a per-render `= []` default for undefined query
    // data churns the geometry effect's dependencies forever. jsdom never
    // caught this class of bug by layout, only by the update-depth crash.
    mockUsePorts.mockReturnValue({ data: undefined, isLoading: true });
    mockUseDeviceConnections.mockReturnValue({ data: undefined, isLoading: true });
    render(<MultiConnectDialog {...baseProps()} />);
    expect(() => pickBothDevices()).not.toThrow();
    expect(screen.getAllByText("Loading ports...")).toHaveLength(2);
  });

  it("filtering narrows a column, and the two empty states are distinct", () => {
    mockUsePorts.mockImplementation((deviceId: string | undefined) => {
      if (deviceId === SOURCE_DEVICE) return { data: SOURCE_PORTS, isLoading: false };
      if (deviceId === TARGET_DEVICE) return { data: EMPTY_PORTS, isLoading: false };
      return { data: EMPTY_PORTS, isLoading: false };
    });
    render(<MultiConnectDialog {...baseProps()} />);
    pickBothDevices();

    // Device B has no ports at all.
    expect(screen.getByText("No ports configured")).toBeInTheDocument();

    const filterInput = screen.getByLabelText("Filter source ports");
    fireEvent.change(filterInput, { target: { value: "eth1" } });
    expect(screen.getByText("eth1")).toBeInTheDocument();
    expect(screen.queryByText("eth2")).not.toBeInTheDocument();

    // Filtered to nothing is NOT the same state as no ports configured.
    fireEvent.change(filterInput, { target: { value: "zzz" } });
    expect(screen.getByText("No ports match")).toBeInTheDocument();

    fireEvent.change(filterInput, { target: { value: "" } });
    expect(screen.getByText("eth2")).toBeInTheDocument();
  });

  it("Connect 1:1 in order pairs only the ports the active filters leave visible", () => {
    render(<MultiConnectDialog {...baseProps()} />);
    pickBothDevices();
    fireEvent.change(screen.getByLabelText("Filter source ports"), { target: { value: "eth1" } });
    fireEvent.change(screen.getByLabelText("Filter target ports"), { target: { value: "0/0/1" } });

    fireEvent.click(screen.getByRole("button", { name: "Connect 1:1 in order" }));

    expect(screen.getByRole("button", { name: "Create 1 connection" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "Review (1)" }));
    const strip = screen.getByTestId("review-strip");
    expect(within(strip).getByText("eth1")).toBeInTheDocument();
    expect(within(strip).getByText("0/0/1")).toBeInTheDocument();
  });

  it("Connect 1:1 in order pairs the remaining free ports and skips staged ones", () => {
    render(<MultiConnectDialog {...baseProps()} />);
    pickBothDevices();
    connectViaClick("eth1", "0/0/1");
    fireEvent.click(screen.getByRole("button", { name: "Connect 1:1 in order" }));
    expect(screen.getByRole("button", { name: "Create 3 connections" })).toBeEnabled();
  });

  it("Connect 1:1 in order states plainly when nothing is left to pair", () => {
    render(<MultiConnectDialog {...baseProps()} />);
    pickBothDevices();
    fireEvent.click(screen.getByRole("button", { name: "Connect 1:1 in order" }));
    fireEvent.click(screen.getByRole("button", { name: "Connect 1:1 in order" }));
    expect(screen.getByText("No free ports left to pair")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create 3 connections" })).toBeEnabled();
  });

  it("removing a line frees both of its ports again", () => {
    render(<MultiConnectDialog {...baseProps()} />);
    pickBothDevices();
    connectViaClick("eth1", "0/0/1");

    fireEvent.click(screen.getByRole("button", { name: "Review (1)" }));
    fireEvent.click(screen.getByLabelText("Remove eth1 to 0/0/1"));

    expect(screen.getByRole("button", { name: "Create connections" })).toBeDisabled();
    expect(within(screen.getByTestId("port-row-sp1")).queryByText("WIRED")).not.toBeInTheDocument();
    // Freed means re-stageable.
    connectViaClick("eth1", "0/0/1");
    expect(screen.getByRole("button", { name: "Create 1 connection" })).toBeEnabled();
  });

  it("the line pill selects a line and its delete button removes it", () => {
    render(<MultiConnectDialog {...baseProps()} />);
    pickBothDevices();
    connectViaClick("eth1", "0/0/1");

    fireEvent.click(screen.getByLabelText("Select line eth1 to 0/0/1"));
    fireEvent.click(screen.getByLabelText("Delete line eth1 to 0/0/1"));
    expect(screen.getByRole("button", { name: "Create connections" })).toBeDisabled();
  });

  it("the same port cannot be connected to itself when one device is on both sides", () => {
    render(<MultiConnectDialog {...baseProps()} />);
    pickBothDevices(DEVICE_A_NAME, DEVICE_A_NAME);
    // Both columns list the same device's ports, so the row testids repeat.
    const [sourceEth1, targetEth1] = screen.getAllByTestId("port-row-sp1");
    clickRow(sourceEth1);
    clickRow(targetEth1);

    expect(screen.getByText("Cannot connect a port to itself")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create connections" })).toBeDisabled();
  });

  it("stamps the batch connection type and notes onto every submitted item", async () => {
    const onSubmit = vi.fn().mockResolvedValue(createdResult(2));
    render(<MultiConnectDialog {...baseProps({ onSubmit })} />);
    pickBothDevices();
    connectViaClick("eth1", "0/0/1");
    connectViaClick("eth3", "0/0/2");

    fireEvent.change(screen.getByLabelText("Connection type"), { target: { value: "fiber" } });
    fireEvent.change(screen.getByLabelText("Notes"), { target: { value: "rack 4 recable" } });
    fireEvent.click(screen.getByRole("button", { name: "Create 2 connections" }));

    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
    expect(onSubmit.mock.calls[0][0]).toEqual([
      {
        device_a_id: SOURCE_DEVICE,
        port_a: "eth1",
        device_b_id: TARGET_DEVICE,
        port_b: "0/0/1",
        connection_type: "fiber",
        notes: "rack 4 recable",
      },
      {
        device_a_id: SOURCE_DEVICE,
        port_a: "eth3",
        device_b_id: TARGET_DEVICE,
        port_b: "0/0/2",
        connection_type: "fiber",
        notes: "rack 4 recable",
      },
    ]);
  });

  it("defaults the type to ethernet and omits notes when the field is blank", async () => {
    const onSubmit = vi.fn().mockResolvedValue(ALL_CREATED);
    render(<MultiConnectDialog {...baseProps({ onSubmit })} />);
    pickBothDevices();
    connectViaClick("eth1", "0/0/1");
    fireEvent.change(screen.getByLabelText("Connection type"), { target: { value: "  " } });
    fireEvent.click(screen.getByRole("button", { name: "Create 1 connection" }));

    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
    const [item] = onSubmit.mock.calls[0][0];
    expect(item.connection_type).toBe("ethernet");
    expect("notes" in item).toBe(false);
  });

  it("a fully created batch reports the created count and clears staging", async () => {
    const onSubmit = vi.fn().mockResolvedValue(createdResult(2));
    const onSuccess = vi.fn();
    render(<MultiConnectDialog {...baseProps({ onSubmit, onSuccess })} />);
    pickBothDevices();
    connectViaClick("eth1", "0/0/1");
    connectViaClick("eth3", "0/0/2");
    fireEvent.click(screen.getByRole("button", { name: "Create 2 connections" }));

    await waitFor(() => expect(onSuccess).toHaveBeenCalledWith(2));
    expect(screen.getByRole("button", { name: "Create connections" })).toBeDisabled();
    expect(screen.queryByTestId("submit-summary")).not.toBeInTheDocument();
  });

  it("PARTIAL SUCCESS is never reported as success: rejected rows stay staged with their reason", async () => {
    const onSubmit = vi.fn().mockResolvedValue({
      created: 1,
      rejected: 1,
      rows: [
        { index: 0, status: "created", connection_id: "c1", error: null },
        {
          index: 1,
          status: "rejected",
          connection_id: null,
          error: "Cannot connect a port to itself",
        },
      ],
    } satisfies BulkConnectionResult);
    const onSuccess = vi.fn();
    render(<MultiConnectDialog {...baseProps({ onSubmit, onSuccess })} />);
    pickBothDevices();
    connectViaClick("eth1", "0/0/1");
    connectViaClick("eth3", "0/0/2");
    fireEvent.click(screen.getByRole("button", { name: "Create 2 connections" }));

    await waitFor(() =>
      expect(screen.getByTestId("submit-summary")).toHaveTextContent(
        "Created 1 of 2 connections, 1 rejected. Fix the flagged lines and retry.",
      ),
    );
    // The success channel never fires for a partial batch.
    expect(onSuccess).not.toHaveBeenCalled();

    // Only the rejected pair is still staged, carrying the server's reason,
    // so a retry re-sends the failure and not the created row.
    expect(screen.getByRole("button", { name: "Create 1 connection" })).toBeEnabled();
    const strip = screen.getByTestId("review-strip");
    expect(within(strip).getByText("eth3")).toBeInTheDocument();
    expect(within(strip).queryByText("eth1")).not.toBeInTheDocument();
    expect(within(strip).getByText("Cannot connect a port to itself")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Create 1 connection" }));
    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(2));
    expect(onSubmit.mock.calls[1][0]).toEqual([
      expect.objectContaining({ port_a: "eth3", port_b: "0/0/2" }),
    ]);
  });

  it("a request failure surfaces the server detail and keeps every line staged", async () => {
    const onSubmit = vi
      .fn()
      .mockRejectedValue({ response: { data: { detail: "Admin role required" } } });
    const onSuccess = vi.fn();
    render(<MultiConnectDialog {...baseProps({ onSubmit, onSuccess })} />);
    pickBothDevices();
    connectViaClick("eth1", "0/0/1");
    connectViaClick("eth3", "0/0/2");
    fireEvent.click(screen.getByRole("button", { name: "Create 2 connections" }));

    await waitFor(() => expect(screen.getByText("Admin role required")).toBeInTheDocument());
    expect(onSuccess).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Create 2 connections" })).toBeEnabled();
  });

  it("refuses to submit a batch past the server cap instead of letting the whole batch fail", async () => {
    const bigSource = ports(210, SOURCE_DEVICE, "sa");
    const bigTarget = ports(210, TARGET_DEVICE, "tb");
    mockUsePorts.mockImplementation((deviceId: string | undefined) => {
      if (deviceId === SOURCE_DEVICE) return { data: bigSource, isLoading: false };
      if (deviceId === TARGET_DEVICE) return { data: bigTarget, isLoading: false };
      return { data: EMPTY_PORTS, isLoading: false };
    });
    const onSubmit = vi.fn().mockResolvedValue(ALL_CREATED);
    render(<MultiConnectDialog {...baseProps({ onSubmit })} />);
    pickBothDevices();
    fireEvent.click(screen.getByRole("button", { name: "Connect 1:1 in order" }));

    fireEvent.click(screen.getByRole("button", { name: "Create 210 connections" }));
    expect(
      screen.getByText("Batches are capped at 200 connections; remove some lines"),
    ).toBeInTheDocument();
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("changing a device clears the staged lines and says so", () => {
    render(<MultiConnectDialog {...baseProps()} />);
    pickBothDevices();
    connectViaClick("eth1", "0/0/1");
    expect(screen.getByRole("button", { name: "Create 1 connection" })).toBeEnabled();

    // The Change button on the A-side picker; the staged port ids belong to
    // the device being replaced, so they cannot survive.
    fireEvent.click(screen.getAllByRole("button", { name: "Change" })[0]);
    expect(
      screen.getByText("Staged connections were cleared because the device changed"),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create connections" })).toBeDisabled();
  });

  it("cancel closes without submitting", () => {
    const onCancel = vi.fn();
    const onSubmit = vi.fn();
    render(<MultiConnectDialog {...baseProps({ onCancel, onSubmit })} />);
    pickBothDevices();
    connectViaClick("eth1", "0/0/1");
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onCancel).toHaveBeenCalled();
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("re-arming: clicking the armed port again disarms it", () => {
    render(<MultiConnectDialog {...baseProps()} />);
    pickBothDevices();
    clickPort("eth1");
    expect(screen.getByText(/selected\. Click a port on the other side/)).toBeInTheDocument();
    clickPort("eth1");
    expect(
      screen.getByText("Drag from one port to another, or click one then the other"),
    ).toBeInTheDocument();
  });

  it("Connect 1:1 in order drops a stale arm instead of leaving it dangling", () => {
    render(<MultiConnectDialog {...baseProps()} />);
    pickBothDevices();
    clickPort("eth1");
    fireEvent.click(screen.getByRole("button", { name: "Connect 1:1 in order" }));
    // eth1 was wired by the bulk action, so the arm pointing at it must go.
    expect(
      screen.queryByText("eth1 selected. Click a port on the other side to connect"),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create 3 connections" })).toBeEnabled();
  });

  it("an armed port that went away in a refetch is re-validated at completion time", () => {
    const props = baseProps();
    const { rerender } = render(<MultiConnectDialog {...props} />);
    pickBothDevices();
    clickPort("eth1");
    expect(
      screen.getByText("eth1 selected. Click a port on the other side to connect"),
    ).toBeInTheDocument();

    // eth1 is deleted from the device and the ports query refetches. The arm
    // still points at it, so completion must refuse rather than stage a line
    // against a port that no longer exists.
    mockUsePorts.mockImplementation((deviceId: string | undefined) => {
      if (deviceId === SOURCE_DEVICE) return { data: SOURCE_PORTS_MINUS_ETH1, isLoading: false };
      if (deviceId === TARGET_DEVICE) return { data: TARGET_PORTS, isLoading: false };
      return { data: EMPTY_PORTS, isLoading: false };
    });
    rerender(<MultiConnectDialog {...props} />);

    clickPort("0/0/1");
    expect(
      screen.getByText("eth1 is no longer available; pick a source port again"),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create connections" })).toBeDisabled();
  });

  it("target ports come from device B, not device A", () => {
    render(<MultiConnectDialog {...baseProps()} />);
    pickBothDevices();
    const targetColumn = screen.getByTestId("port-column-target");
    expect(within(targetColumn).getByText("0/0/1")).toBeInTheDocument();
    expect(within(targetColumn).queryByText("eth1")).not.toBeInTheDocument();
    expect(TARGET_PORTS.map((p) => p.name)).toContain("0/0/1");
  });
});
