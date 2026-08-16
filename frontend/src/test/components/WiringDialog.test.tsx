import { render, screen, fireEvent, within } from "@testing-library/react";
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

const mockUsePorts = vi.fn();
const mockUseDeviceConnections = vi.fn();

vi.mock("@/api/ports", () => ({
  usePorts: (...args: unknown[]) => mockUsePorts(...args),
}));
vi.mock("@/api/connections", () => ({
  useDeviceConnections: (...args: unknown[]) => mockUseDeviceConnections(...args),
}));

import { WiringDialog } from "@/components/topology-editor/WiringDialog";

const OTHER_DEVICE = "device-other";

// The HERD fabric shape (issue #517 review item 1): DUT ports patch into an
// L1 edge switch, so a DUT-to-DUT canvas edge's ports are each cabled to a
// THIRD device, never directly to each other. eth2 and 0/0/3 are both
// cabled to OTHER_DEVICE below, not to SOURCE_DEVICE/TARGET_DEVICE.
const SOURCE_CONNS = [connectionFixture("c1", SOURCE_DEVICE, "eth2", OTHER_DEVICE, "x1")];
const TARGET_CONNS = [connectionFixture("c2", OTHER_DEVICE, "y1", TARGET_DEVICE, "0/0/3")];

function baseProps(overrides: Partial<React.ComponentProps<typeof WiringDialog>> = {}) {
  return {
    open: true,
    sourceDeviceId: SOURCE_DEVICE,
    sourceDeviceName: "dut-01",
    sourceTopologyType: "PHYSICAL" as const,
    targetDeviceId: TARGET_DEVICE,
    targetDeviceName: "switch-01",
    targetTopologyType: "PHYSICAL" as const,
    defaultLayer: "L2" as const,
    onConfirm: vi.fn(),
    onCancel: vi.fn(),
    ...overrides,
  };
}

function rowFor(text: string): HTMLElement {
  return screen.getByText(text).closest("[data-port-id]") as HTMLElement;
}

// Full mousedown/mouseup/click sequence on the SAME row, with no intervening
// mousemove (issue #517 review item 2): this is what a real plain click
// dispatches, and is the only path the component treats as "click to
// arm/complete", so tests exercise the actual gesture instead of relying on
// a bare fireEvent.click (which the previous, buggy implementation's tests
// masked a real re-arm/disarm bug).
function clickPort(text: string) {
  const row = rowFor(text);
  fireEvent.mouseDown(row, { clientX: 0, clientY: 0 });
  fireEvent.mouseUp(row, { clientX: 0, clientY: 0 });
  fireEvent.click(row);
}

function connectViaClick(sourceText: string, targetText: string) {
  clickPort(sourceText);
  clickPort(targetText);
}

// mousedown on the source row, a mousemove past the drag threshold, then
// mouseup on the target row: the actual drag path, distinct from a click.
function dragPort(fromText: string, toText: string) {
  const fromRow = rowFor(fromText);
  const toRow = rowFor(toText);
  fireEvent.mouseDown(fromRow, { clientX: 0, clientY: 0 });
  fireEvent.mouseMove(window, { clientX: 50, clientY: 50 });
  fireEvent.mouseUp(toRow, { clientX: 50, clientY: 50 });
}

describe("WiringDialog", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockStandardPorts(mockUsePorts);
    mockUseDeviceConnections.mockImplementation((deviceId: string) => {
      if (deviceId === SOURCE_DEVICE) return { data: SOURCE_CONNS, isLoading: false };
      if (deviceId === TARGET_DEVICE) return { data: TARGET_CONNS, isLoading: false };
      return { data: EMPTY_CONNECTIONS, isLoading: false };
    });
  });

  it("renders both port columns with monospace rows; a cabled port carries an informational CABLED tag but stays selectable", () => {
    render(<WiringDialog {...baseProps()} />);
    expect(screen.getByText("eth1")).toBeInTheDocument();
    expect(screen.getByText("0/0/1")).toBeInTheDocument();

    const eth2Row = screen.getByTestId("port-row-sp2");
    expect(within(eth2Row).getByText("CABLED")).toBeInTheDocument();
    expect(eth2Row.getAttribute("data-status")).toBe("free");
    expect(eth2Row).toHaveAttribute("tabindex", "0");
  });

  it("an uncabled free port carries the quiet informational 'no cable' tag (review round 3 item 6)", () => {
    render(<WiringDialog {...baseProps()} />);
    // eth1 has no registered connection at all in this fixture set.
    const eth1Row = screen.getByTestId("port-row-sp1");
    expect(within(eth1Row).getByText("no cable")).toBeInTheDocument();
    expect(within(eth1Row).queryByText("CABLED")).not.toBeInTheDocument();
    // eth2 IS cabled: the two tags are mutually exclusive.
    const eth2Row = screen.getByTestId("port-row-sp2");
    expect(within(eth2Row).queryByText("no cable")).not.toBeInTheDocument();
  });

  it("the review strip shows the uncabled status label restored via resolveEdgeStroke (review round 3 item 6)", () => {
    render(<WiringDialog {...baseProps()} />);
    connectViaClick("eth1", "0/0/1"); // both uncabled
    fireEvent.click(screen.getByRole("button", { name: "Review (1)" }));
    const strip = screen.getByTestId("review-strip");
    expect(within(strip).getByText("uncabled port")).toBeInTheDocument();
  });

  it("a cabled port is fully wireable (not blocked): the fabric shape lands portsCabled true", () => {
    const onConfirm = vi.fn();
    render(<WiringDialog {...baseProps({ onConfirm })} />);
    connectViaClick("eth2", "0/0/3");
    fireEvent.click(screen.getByRole("button", { name: "Add 1 connection" }));

    expect(onConfirm).toHaveBeenCalledWith([
      expect.objectContaining({ sourcePortId: "sp2", targetPortId: "tp3", portsCabled: true }),
    ]);
  });

  it("an uncabled pair still lands portsCabled false, the existing soft warning", () => {
    const onConfirm = vi.fn();
    render(<WiringDialog {...baseProps({ onConfirm })} />);
    connectViaClick("eth1", "0/0/1");
    fireEvent.click(screen.getByRole("button", { name: "Add 1 connection" }));

    expect(onConfirm).toHaveBeenCalledWith([
      expect.objectContaining({ sourcePortId: "sp1", targetPortId: "tp1", portsCabled: false }),
    ]);
  });

  it("click-to-connect: arming shows the hint, completing wires the pair and tags both ports WIRED", () => {
    render(<WiringDialog {...baseProps()} />);
    clickPort("eth1");
    expect(screen.getByText("eth1 selected. Click a target port to connect")).toBeInTheDocument();

    clickPort("0/0/1");
    expect(screen.getByRole("button", { name: "Add 1 connection" })).toBeEnabled();

    const sourceRow = screen.getByTestId("port-row-sp1");
    expect(within(sourceRow).getByText("WIRED")).toBeInTheDocument();
    const targetRow = screen.getByTestId("port-row-tp1");
    expect(within(targetRow).getByText("WIRED")).toBeInTheDocument();
  });

  it("clicking the armed port again disarms it", () => {
    render(<WiringDialog {...baseProps()} />);
    clickPort("eth1");
    expect(screen.getByText(/selected\. Click a target port/)).toBeInTheDocument();
    clickPort("eth1");
    expect(
      screen.getByText("Drag from a source port to a target port, or click one then the other"),
    ).toBeInTheDocument();
  });

  it("clicking a session-wired port a second time shows the session error and does not add another line", () => {
    render(<WiringDialog {...baseProps()} />);
    connectViaClick("eth1", "0/0/1");
    clickPort("eth1");
    expect(screen.getByText("eth1 already has a line in this session")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add 1 connection" })).toBeEnabled();
  });

  it("keyboard: Enter on a free row arms it, Enter on the opposite free row completes the connection", () => {
    render(<WiringDialog {...baseProps()} />);
    const sourceRow = screen.getByTestId("port-row-sp1");
    sourceRow.focus();
    fireEvent.keyDown(sourceRow, { key: "Enter" });
    expect(screen.getByText("eth1 selected. Click a target port to connect")).toBeInTheDocument();

    const targetRow = screen.getByTestId("port-row-tp1");
    targetRow.focus();
    fireEvent.keyDown(targetRow, { key: " " });
    expect(screen.getByRole("button", { name: "Add 1 connection" })).toBeEnabled();
  });

  it("drag path (mousedown, mousemove past the threshold, mouseup on a free opposite port) completes a connection", () => {
    render(<WiringDialog {...baseProps()} />);
    dragPort("eth3", "0/0/2");
    expect(screen.getByRole("button", { name: "Add 1 connection" })).toBeEnabled();
  });

  it("mid-drag (past the threshold, before mouseup) shows the release hint, reachable now that a distinct dragging state exists (review item 9)", () => {
    render(<WiringDialog {...baseProps()} />);
    const fromRow = rowFor("eth1");
    fireEvent.mouseDown(fromRow, { clientX: 0, clientY: 0 });
    expect(
      screen.getByText("Drag from a source port to a target port, or click one then the other"),
    ).toBeInTheDocument();

    fireEvent.mouseMove(window, { clientX: 50, clientY: 50 });
    expect(screen.getByText("Release on a highlighted port")).toBeInTheDocument();

    fireEvent.mouseUp(rowFor("0/0/1"), { clientX: 50, clientY: 50 });
    expect(screen.getByRole("button", { name: "Add 1 connection" })).toBeEnabled();
  });

  it("dragging and releasing off any port cancels without creating a connection", () => {
    render(<WiringDialog {...baseProps()} />);
    const fromRow = rowFor("eth1");
    fireEvent.mouseDown(fromRow, { clientX: 0, clientY: 0 });
    fireEvent.mouseMove(window, { clientX: 50, clientY: 50 });
    fireEvent.mouseUp(document.body, { clientX: 50, clientY: 50 });
    expect(screen.getByRole("button", { name: "Add connections" })).toBeDisabled();
  });

  it("while connections are loading, ports are inert, 1:1 is disabled, and confirm stays disabled", () => {
    mockUseDeviceConnections.mockImplementation((deviceId: string) => {
      if (deviceId === SOURCE_DEVICE) return { data: EMPTY_CONNECTIONS, isLoading: true };
      if (deviceId === TARGET_DEVICE) return { data: EMPTY_CONNECTIONS, isLoading: false };
      return { data: EMPTY_CONNECTIONS, isLoading: false };
    });
    render(<WiringDialog {...baseProps()} />);
    clickPort("eth1");
    clickPort("0/0/1");
    expect(screen.getByRole("button", { name: "Add connections" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Connect 1:1 in order" })).toBeDisabled();
    expect(screen.queryByText("WIRED")).not.toBeInTheDocument();
  });

  it("mounts without an infinite render loop when data is undefined and isLoading is true on both hooks, before any query has resolved (review round 3 item 1)", () => {
    // This is the exact case every prior fixture missed: mockStandardPorts/
    // mockNoConnections (and every ad hoc override above) always return a
    // concrete `data` array. A real TanStack Query hook's FIRST render, before
    // the network request resolves, returns `data: undefined`. The
    // destructuring default `{ data: x = [] }` re-evaluates a fresh `[]` on
    // every render for as long as `data` stays undefined, which (before the
    // module-level NO_PORTS/NO_CONNECTIONS fix) made portIndexBySide churn
    // every render and crashed with "Maximum update depth exceeded" on the
    // very first mount.
    mockUsePorts.mockReturnValue({ data: undefined, isLoading: true });
    mockUseDeviceConnections.mockReturnValue({ data: undefined, isLoading: true });
    expect(() => render(<WiringDialog {...baseProps()} />)).not.toThrow();
    expect(screen.getAllByText("Loading ports...")).toHaveLength(2);
  });

  it("per-line layer control recolors the line and becomes the default for new lines", () => {
    render(<WiringDialog {...baseProps()} />);
    connectViaClick("eth1", "0/0/1");

    const [lineId] = Array.from(document.querySelectorAll("[data-testid^='line-pill-']")).map(
      (el) => el.getAttribute("data-testid")!.replace("line-pill-", ""),
    );
    fireEvent.click(screen.getByTestId(`line-pill-${lineId}`));
    fireEvent.click(screen.getByTestId(`line-layer-${lineId}-L3`));

    expect(screen.getByTestId(`line-layer-${lineId}-L3`)).toHaveAttribute("aria-pressed", "true");
    const sourceDot = screen.getByTestId("port-dot-sp1");
    expect(sourceDot).toHaveStyle({ background: "rgb(34, 197, 94)" });

    connectViaClick("eth3", "0/0/2");
    const newPill = screen.getByTestId(
      Array.from(document.querySelectorAll("[data-testid^='line-pill-']"))
        .map((el) => el.getAttribute("data-testid")!)
        .find((id) => id !== `line-pill-${lineId}`)!,
    );
    expect(newPill).toHaveTextContent("L3");
  });

  it("deleting a line frees both ports for reselection", () => {
    render(<WiringDialog {...baseProps()} />);
    connectViaClick("eth1", "0/0/1");
    const [lineId] = Array.from(document.querySelectorAll("[data-testid^='line-pill-']")).map(
      (el) => el.getAttribute("data-testid")!.replace("line-pill-", ""),
    );
    fireEvent.click(screen.getByTestId(`line-pill-${lineId}`));
    fireEvent.click(screen.getByLabelText(`Delete line eth1 to 0/0/1`));

    expect(screen.getByRole("button", { name: "Add connections" })).toBeDisabled();
    const sourceRow = screen.getByTestId("port-row-sp1");
    expect(within(sourceRow).queryByText("WIRED")).not.toBeInTheDocument();
  });

  it("filters narrow a column, clearing restores it, and a no-match filter shows the quiet empty line", () => {
    render(<WiringDialog {...baseProps()} />);
    const filterInput = screen.getByLabelText("Filter source ports");
    fireEvent.change(filterInput, { target: { value: "eth1" } });
    expect(screen.getByText("eth1")).toBeInTheDocument();
    expect(screen.queryByText("eth2")).not.toBeInTheDocument();

    fireEvent.change(filterInput, { target: { value: "zzz" } });
    expect(screen.getByText("No ports match")).toBeInTheDocument();

    fireEvent.change(filterInput, { target: { value: "" } });
    expect(screen.getByText("eth1")).toBeInTheDocument();
    expect(screen.getByText("eth2")).toBeInTheDocument();
  });

  it("a connection to a now-filtered-out port survives in the review list", () => {
    render(<WiringDialog {...baseProps()} />);
    connectViaClick("eth1", "0/0/1");
    fireEvent.change(screen.getByLabelText("Filter source ports"), { target: { value: "eth3" } });
    expect(screen.queryByText("eth1")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Review (1)" }));
    const strip = screen.getByTestId("review-strip");
    expect(within(strip).getByText("eth1")).toBeInTheDocument();
    expect(within(strip).getByText("0/0/1")).toBeInTheDocument();
  });

  it("connect 1:1 in order pairs free ports top to bottom, skipping session-wired", () => {
    render(<WiringDialog {...baseProps()} />);
    connectViaClick("eth1", "0/0/1");
    fireEvent.click(screen.getByRole("button", { name: "Connect 1:1 in order" }));
    expect(screen.getByRole("button", { name: "Add 3 connections (keeps 1 per pair)" })).toBeEnabled();
  });

  it("connect 1:1 in order shows the blunt error when nothing can pair", () => {
    mockUsePorts.mockImplementation((deviceId: string) => {
      if (deviceId === SOURCE_DEVICE) return { data: SOURCE_PORTS, isLoading: false };
      if (deviceId === TARGET_DEVICE) return { data: EMPTY_PORTS, isLoading: false };
      return { data: EMPTY_PORTS, isLoading: false };
    });
    render(<WiringDialog {...baseProps()} />);
    fireEvent.click(screen.getByRole("button", { name: "Connect 1:1 in order" }));
    expect(screen.getByText("No free ports left to pair")).toBeInTheDocument();
  });

  it("connect 1:1 in order respects each column's active filter (review item 7)", () => {
    render(<WiringDialog {...baseProps()} />);
    fireEvent.change(screen.getByLabelText("Filter source ports"), { target: { value: "eth1" } });
    fireEvent.change(screen.getByLabelText("Filter target ports"), { target: { value: "0/0/1" } });

    fireEvent.click(screen.getByRole("button", { name: "Connect 1:1 in order" }));

    // Only the one visible pair on each side wires, not eth2/eth3 or 0/0/2/0/0/3
    // which are hidden by the filters.
    expect(screen.getByRole("button", { name: "Add 1 connection" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "Review (1)" }));
    const strip = screen.getByTestId("review-strip");
    expect(within(strip).getByText("eth1")).toBeInTheDocument();
    expect(within(strip).getByText("0/0/1")).toBeInTheDocument();
  });

  it("connect 1:1 in order clears a stale arm on success (review item 6)", () => {
    render(<WiringDialog {...baseProps()} />);
    // Arm eth1 (free) via a click, but do not complete it yet.
    clickPort("eth1");
    expect(screen.getByText("eth1 selected. Click a target port to connect")).toBeInTheDocument();

    // 1:1 in order wires everything free, including eth1; the stale arm
    // must not linger as if nothing happened.
    fireEvent.click(screen.getByRole("button", { name: "Connect 1:1 in order" }));
    expect(screen.getByRole("button", { name: "Add 3 connections (keeps 1 per pair)" })).toBeEnabled();
    expect(
      screen.queryByText("eth1 selected. Click a target port to connect"),
    ).not.toBeInTheDocument();
    expect(
      screen.getByText("Drag from a source port to a target port, or click one then the other"),
    ).toBeInTheDocument();
  });

  it("the armed-port invariant: a port that became unavailable after being armed is re-validated, not silently reused (review item 6)", () => {
    // A port can go stale while armed without ever passing through this
    // dialog's own complete/1:1 paths, e.g. an existing canvas edge
    // appearing concurrently (the existingWiredSourcePortIds prop changing
    // while the dialog stays open).
    const { rerender } = render(<WiringDialog {...baseProps()} />);
    clickPort("eth1");
    expect(screen.getByText("eth1 selected. Click a target port to connect")).toBeInTheDocument();

    rerender(<WiringDialog {...baseProps({ existingWiredSourcePortIds: new Set(["sp1"]) })} />);

    clickPort("0/0/1");
    expect(
      screen.getByText("eth1 is no longer available; pick a source port again"),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add connections" })).toBeDisabled();
  });

  it("existing canvas-wired ports render WIRED and are unavailable, with a distinct tooltip and error (review item 8)", () => {
    render(
      <WiringDialog
        {...baseProps({
          existingWiredSourcePortIds: new Set(["sp1"]),
          existingWiredTargetPortIds: new Set(["tp1"]),
        })}
      />,
    );
    const sourceRow = screen.getByTestId("port-row-sp1");
    expect(within(sourceRow).getByText("WIRED")).toBeInTheDocument();
    expect(sourceRow).toHaveAttribute("title", "already connected on the canvas");

    clickPort("eth1");
    expect(screen.getByText("eth1 is already connected on the canvas")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add connections" })).toBeDisabled();
  });

  it("existing canvas-wired ports are excluded from Connect 1:1 in order", () => {
    render(<WiringDialog {...baseProps({ existingWiredSourcePortIds: new Set(["sp1"]) })} />);
    fireEvent.click(screen.getByRole("button", { name: "Connect 1:1 in order" }));
    // eth1 is canvas-wired: only eth2/eth3 (2 free) pair, not 3.
    expect(screen.getByRole("button", { name: "Add 2 connections (keeps 1 per pair)" })).toBeEnabled();
  });

  it("a device with no ports shows the empty state, not a blank list (review item 9)", () => {
    mockUsePorts.mockImplementation((deviceId: string) => {
      if (deviceId === SOURCE_DEVICE) return { data: EMPTY_PORTS, isLoading: false };
      if (deviceId === TARGET_DEVICE) return { data: TARGET_PORTS, isLoading: false };
      return { data: EMPTY_PORTS, isLoading: false };
    });
    render(<WiringDialog {...baseProps()} />);
    expect(screen.getByText("No ports configured")).toBeInTheDocument();
  });

  it("review strip toggles and its remove button removes a line", () => {
    render(<WiringDialog {...baseProps()} />);
    connectViaClick("eth1", "0/0/1");
    fireEvent.click(screen.getByRole("button", { name: "Review (1)" }));
    fireEvent.click(screen.getByLabelText("Remove eth1 to 0/0/1"));
    expect(screen.getByRole("button", { name: "Add connections" })).toBeDisabled();
  });

  it("the provisioning notice appears in the footer once more than one line is staged, mode-independent (review round 3 item 4)", () => {
    render(<WiringDialog {...baseProps()} />);
    expect(screen.queryByTestId("provisioning-notice")).not.toBeInTheDocument();

    connectViaClick("eth1", "0/0/1");
    // Still absent at exactly one line.
    expect(screen.queryByTestId("provisioning-notice")).not.toBeInTheDocument();

    connectViaClick("eth3", "0/0/2");
    expect(screen.getByTestId("provisioning-notice")).toHaveTextContent(
      "Provisioning currently keeps one connection per device pair",
    );
    // Visible without ever opening Review.
    expect(screen.queryByTestId("review-strip")).not.toBeInTheDocument();
  });

  it("staging more than one line puts the caveat in the confirm button label itself (review round 3 item 4)", () => {
    render(<WiringDialog {...baseProps()} />);
    connectViaClick("eth1", "0/0/1");
    expect(screen.getByRole("button", { name: "Add 1 connection" })).toBeInTheDocument();

    connectViaClick("eth3", "0/0/2");
    expect(
      screen.getByRole("button", { name: "Add 2 connections (keeps 1 per pair)" }),
    ).toBeInTheDocument();
  });

  it("confirm is disabled at zero lines and labels pluralize correctly", () => {
    render(<WiringDialog {...baseProps()} />);
    expect(screen.getByRole("button", { name: "Add connections" })).toBeDisabled();

    connectViaClick("eth1", "0/0/1");
    expect(screen.getByRole("button", { name: "Add 1 connection" })).toBeEnabled();

    connectViaClick("eth3", "0/0/2");
    expect(screen.getByRole("button", { name: "Add 2 connections (keeps 1 per pair)" })).toBeEnabled();
  });

  it("confirm calls onConfirm once with every session line's port ids, names, and layer", () => {
    const onConfirm = vi.fn();
    render(<WiringDialog {...baseProps({ onConfirm })} />);
    connectViaClick("eth1", "0/0/1");
    connectViaClick("eth3", "0/0/2");

    fireEvent.click(screen.getByRole("button", { name: "Add 2 connections (keeps 1 per pair)" }));

    expect(onConfirm).toHaveBeenCalledTimes(1);
    const lines = onConfirm.mock.calls[0][0];
    expect(lines).toHaveLength(2);
    expect(lines).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          sourcePortId: "sp1",
          sourcePortName: "eth1",
          targetPortId: "tp1",
          targetPortName: "0/0/1",
          layer: "L2",
        }),
        expect.objectContaining({
          sourcePortId: "sp3",
          sourcePortName: "eth3",
          targetPortId: "tp2",
          targetPortName: "0/0/2",
          layer: "L2",
        }),
      ]),
    );
  });

  it("cancel invokes onCancel without confirming", () => {
    const onCancel = vi.fn();
    const onConfirm = vi.fn();
    render(<WiringDialog {...baseProps({ onCancel, onConfirm })} />);
    connectViaClick("eth1", "0/0/1");
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onCancel).toHaveBeenCalled();
    expect(onConfirm).not.toHaveBeenCalled();
  });
});
