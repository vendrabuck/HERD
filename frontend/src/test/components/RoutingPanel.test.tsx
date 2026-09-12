import { render, screen, fireEvent, act, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, beforeAll, beforeEach } from "vitest";
import type { Node } from "@xyflow/react";

import { useTopologyStore } from "@/stores/topologyStore";
import type { DeviceNodeData, L3RouteIntent } from "@/types/topology.types";
import type { ResolvedRouteProblem } from "@/lib/l3";

const { toastError } = vi.hoisted(() => ({ toastError: vi.fn() }));
vi.mock("react-hot-toast", () => ({
  default: Object.assign((..._args: unknown[]) => {}, { error: toastError }),
}));

// The two useDeviceConfigVersions/useDeviceConfigVersion hooks are mocked so
// the panel's Import action can be driven deterministically without a real
// QueryClient/HTTP layer; each test overrides these hoisted mock functions'
// return values directly.
const { versionsQueryMock, versionQueryMock } = vi.hoisted(() => ({
  versionsQueryMock: vi.fn(),
  versionQueryMock: vi.fn(),
}));
vi.mock("@/api/deviceConfig", () => ({
  useDeviceConfigVersions: (...args: unknown[]) => versionsQueryMock(...args),
  useDeviceConfigVersion: (...args: unknown[]) => versionQueryMock(...args),
}));

import { RoutingPanel } from "@/components/topology-editor/RoutingPanel";

const NODE_ID = "n-l3";
const DEVICE_ID = "dev-l3-1";

function route(overrides: Partial<L3RouteIntent> = {}): L3RouteIntent {
  return {
    destination: "10.0.0.0/24",
    next_hop: null,
    interface: "eth0",
    virtual_router: null,
    ...overrides,
  };
}

function problem(overrides: Partial<ResolvedRouteProblem> = {}): ResolvedRouteProblem {
  return {
    node_id: NODE_ID,
    route: null,
    reason: "l3_switch_unattached",
    detail: null,
    ...overrides,
  };
}

function makeNode(routes: L3RouteIntent[] | undefined): Node<DeviceNodeData> {
  return {
    id: NODE_ID,
    type: "deviceNode",
    position: { x: 0, y: 0 },
    data: {
      device: { id: DEVICE_ID, name: "core-sw-1", connection_type: "Layer 3 Switch" },
      label: "core-sw-1",
      topologyType: "PHYSICAL",
      ...(routes !== undefined ? { l3: { routes } } : {}),
    } as DeviceNodeData,
  };
}

// Review fix F6: a malformed `data.l3` a bulk import or external PUT can
// store, which `makeNode`'s always-well-formed `routes` param cannot express.
function makeMalformedNode(l3: unknown): Node<DeviceNodeData> {
  return {
    id: NODE_ID,
    type: "deviceNode",
    position: { x: 0, y: 0 },
    data: {
      device: { id: DEVICE_ID, name: "core-sw-1", connection_type: "Layer 3 Switch" },
      label: "core-sw-1",
      topologyType: "PHYSICAL",
      l3,
    } as DeviceNodeData,
  };
}

function seedStoreAndRender(routes: L3RouteIntent[] | undefined, problems: ResolvedRouteProblem[] = []) {
  const node = makeNode(routes);
  act(() => {
    useTopologyStore.setState({ nodes: [node], edges: [], selectedEdgeLayer: "L2" });
  });
  const view = render(<RoutingPanel node={node} problems={problems} />);
  return { view, node };
}

function storeRoutes(): L3RouteIntent[] | undefined {
  const data = useTopologyStore.getState().nodes[0]?.data as DeviceNodeData | undefined;
  return data?.l3?.routes;
}

// Review fix F10: the detail query is now lazy (`enabled: false`), fetched
// only via an explicit `refetch()` call from the Import handler. Returns the
// `refetch` mock so a test can assert it resolved/rejected as expected.
function mockVersionDetail(result: { data?: unknown; isError?: boolean; error?: unknown }) {
  const refetch = vi.fn().mockResolvedValue(result);
  versionQueryMock.mockReturnValue({ data: undefined, isFetching: false, refetch });
  return refetch;
}

beforeAll(() => {
  HTMLDialogElement.prototype.showModal = vi.fn(function (this: HTMLDialogElement) {
    this.open = true;
  });
  HTMLDialogElement.prototype.close = vi.fn(function (this: HTMLDialogElement) {
    this.open = false;
  });
});

beforeEach(() => {
  useTopologyStore.setState({ nodes: [], edges: [], selectedEdgeLayer: "L2" });
  toastError.mockClear();
  versionsQueryMock.mockReturnValue({ data: { items: [] }, isFetching: false });
  mockVersionDetail({ data: undefined });
});

describe("RoutingPanel", () => {
  it("shows the device name and the empty state when there is no routing intent", () => {
    seedStoreAndRender(undefined);
    expect(screen.getByText("core-sw-1")).toBeTruthy();
    expect(screen.getByText(/No routing intent/)).toBeTruthy();
  });

  it("disables Add route until both destination and interface are filled", () => {
    seedStoreAndRender([]);
    const addButton = screen.getByRole("button", { name: "Add route" });
    expect(addButton).toBeDisabled();

    fireEvent.change(screen.getByLabelText("New destination"), { target: { value: "10.0.0.0/24" } });
    expect(addButton).toBeDisabled();

    fireEvent.change(screen.getByLabelText("New interface"), { target: { value: "eth0" } });
    expect(addButton).not.toBeDisabled();
  });

  it("adding a route writes it through the store, trimmed, with blank next hop stored as null", () => {
    seedStoreAndRender([]);
    fireEvent.change(screen.getByLabelText("New destination"), { target: { value: "  10.0.0.0/24  " } });
    fireEvent.change(screen.getByLabelText("New interface"), { target: { value: " eth0 " } });
    fireEvent.click(screen.getByRole("button", { name: "Add route" }));

    expect(storeRoutes()).toEqual([
      { destination: "10.0.0.0/24", interface: "eth0", next_hop: null, virtual_router: null },
    ]);
  });

  it("adding a route with a next hop and virtual router fills both", () => {
    seedStoreAndRender([]);
    fireEvent.change(screen.getByLabelText("New destination"), { target: { value: "10.0.0.0/24" } });
    fireEvent.change(screen.getByLabelText("New next hop"), { target: { value: "10.0.0.1" } });
    fireEvent.change(screen.getByLabelText("New interface"), { target: { value: "eth0" } });
    fireEvent.change(screen.getByLabelText("New virtual router"), { target: { value: "vr1" } });
    fireEvent.click(screen.getByRole("button", { name: "Add route" }));

    expect(storeRoutes()).toEqual([
      { destination: "10.0.0.0/24", interface: "eth0", next_hop: "10.0.0.1", virtual_router: "vr1" },
    ]);
  });

  // Review fix F2: existing rows now edit in local draft state, committing
  // only on blur or Enter.
  describe("existing-row editing (review fix F2)", () => {
    it("does not commit to the store until blur", () => {
      seedStoreAndRender([route({ destination: "10.0.0.0/24" })]);
      fireEvent.change(screen.getByLabelText("Destination"), { target: { value: "10.0.1.0/24" } });
      expect(storeRoutes()?.[0].destination).toBe("10.0.0.0/24");
      fireEvent.blur(screen.getByLabelText("Destination"));
      expect(storeRoutes()?.[0].destination).toBe("10.0.1.0/24");
    });

    it("commits on Enter", () => {
      seedStoreAndRender([route({ destination: "10.0.0.0/24" })]);
      const input = screen.getByLabelText("Destination");
      fireEvent.change(input, { target: { value: "10.0.1.0/24" } });
      fireEvent.keyDown(input, { key: "Enter" });
      expect(storeRoutes()?.[0].destination).toBe("10.0.1.0/24");
    });

    it("blanking next hop and committing stores null, not an empty string", () => {
      seedStoreAndRender([route({ next_hop: "10.0.0.1" })]);
      fireEvent.change(screen.getByLabelText("Next hop"), { target: { value: "" } });
      fireEvent.blur(screen.getByLabelText("Next hop"));
      expect(storeRoutes()?.[0].next_hop).toBeNull();
    });

    it("clearing destination then blurring does not write an empty string to the store, and shows an inline error", () => {
      seedStoreAndRender([route({ destination: "10.0.0.0/24" })]);
      fireEvent.change(screen.getByLabelText("Destination"), { target: { value: "" } });
      fireEvent.blur(screen.getByLabelText("Destination"));
      expect(storeRoutes()?.[0].destination).toBe("10.0.0.0/24");
      expect(screen.getByText("Destination and interface are required")).toBeTruthy();
    });

    it("clearing interface then blurring does not write an empty string to the store", () => {
      seedStoreAndRender([route({ interface: "eth0" })]);
      fireEvent.change(screen.getByLabelText("Interface"), { target: { value: "" } });
      fireEvent.blur(screen.getByLabelText("Interface"));
      expect(storeRoutes()?.[0].interface).toBe("eth0");
    });

    it("refuses a 65th character", () => {
      seedStoreAndRender([route()]);
      const longValue = "a".repeat(70);
      fireEvent.change(screen.getByLabelText("Destination"), { target: { value: longValue } });
      expect((screen.getByLabelText("Destination") as HTMLInputElement).value).toHaveLength(64);
    });
  });

  it("removing the only row writes an empty list, which the store turns into no intent", () => {
    seedStoreAndRender([route()]);
    fireEvent.click(screen.getByRole("button", { name: "Remove route 0" }));
    expect(storeRoutes()).toBeUndefined();
  });

  it("removing one of two rows leaves the other", () => {
    seedStoreAndRender([route({ interface: "eth0" }), route({ interface: "eth1" })]);
    fireEvent.click(screen.getByRole("button", { name: "Remove route 0" }));
    expect(storeRoutes()).toEqual([route({ interface: "eth1" })]);
  });

  it("disables Import when the switch has no config version", () => {
    seedStoreAndRender([]);
    expect(screen.getByRole("button", { name: "Import from device config" })).toBeDisabled();
  });

  describe("Import (review fixes F3, F10)", () => {
    it("fetches the config version lazily: no detail query until Import is clicked", () => {
      const refetch = mockVersionDetail({ data: undefined });
      versionsQueryMock.mockReturnValue({ data: { items: [{ id: "cv-1" }] }, isFetching: false });
      seedStoreAndRender([]);
      expect(refetch).not.toHaveBeenCalled();
    });

    it("replaces an empty table directly, with no confirm", async () => {
      versionsQueryMock.mockReturnValue({ data: { items: [{ id: "cv-1" }] }, isFetching: false });
      mockVersionDetail({
        data: { config: { routes: [{ destination: "10.9.9.0/24", interface: "eth2" }] } },
      });
      seedStoreAndRender([]);
      fireEvent.click(screen.getByRole("button", { name: "Import from device config" }));

      await waitFor(() =>
        expect(storeRoutes()).toEqual([
          { destination: "10.9.9.0/24", interface: "eth2", next_hop: null, virtual_router: null },
        ]),
      );
    });

    it("asks for confirmation and REPLACES the table when rows already exist", async () => {
      versionsQueryMock.mockReturnValue({ data: { items: [{ id: "cv-1" }] }, isFetching: false });
      mockVersionDetail({
        data: {
          config: { routes: [{ destination: "10.9.9.0/24", interface: "eth2", next_hop: "10.9.9.1" }] },
        },
      });
      seedStoreAndRender([route({ interface: "eth0" })]);

      fireEvent.click(screen.getByRole("button", { name: "Import from device config" }));
      // waitFor on the TEXT alone is not enough here: ConfirmDialog renders
      // its content unconditionally (native <dialog>, visibility driven by
      // an imperative showModal() in its own effect), so the title text
      // commits to the DOM a render before that effect flushes and the
      // dialog is actually open. getByRole computes accessible names off
      // the live accessibility tree, which excludes a still-closed
      // <dialog>'s contents, so waiting on the ROLE query itself (not just
      // the text) is what guarantees the button is really clickable.
      await waitFor(() => expect(screen.getByRole("button", { name: "Replace" })).toBeTruthy());
      // Not applied yet.
      expect(storeRoutes()).toEqual([route({ interface: "eth0" })]);

      fireEvent.click(screen.getByRole("button", { name: "Replace" }));
      expect(storeRoutes()).toEqual([
        { destination: "10.9.9.0/24", interface: "eth2", next_hop: "10.9.9.1", virtual_router: null },
      ]);
    });

    it("cancel leaves the existing table untouched", async () => {
      versionsQueryMock.mockReturnValue({ data: { items: [{ id: "cv-1" }] }, isFetching: false });
      mockVersionDetail({
        data: { config: { routes: [{ destination: "10.9.9.0/24", interface: "eth2" }] } },
      });
      seedStoreAndRender([route({ interface: "eth0" })]);

      fireEvent.click(screen.getByRole("button", { name: "Import from device config" }));
      // See the comment above: wait on the role query itself, not just text.
      await waitFor(() => expect(screen.getByRole("button", { name: "Cancel" })).toBeTruthy());
      fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
      expect(storeRoutes()).toEqual([route({ interface: "eth0" })]);
    });

    it("an empty routes list toasts and changes nothing (review fix F3)", async () => {
      versionsQueryMock.mockReturnValue({ data: { items: [{ id: "cv-1" }] }, isFetching: false });
      mockVersionDetail({ data: { config: { routes: [] } } });
      seedStoreAndRender([route({ interface: "eth0" })]);

      fireEvent.click(screen.getByRole("button", { name: "Import from device config" }));
      await waitFor(() => expect(toastError).toHaveBeenCalledWith("Latest config version has no routes"));
      expect(storeRoutes()).toEqual([route({ interface: "eth0" })]);
    });

    it("a config with no routes key toasts and changes nothing (review fix F3)", async () => {
      versionsQueryMock.mockReturnValue({ data: { items: [{ id: "cv-1" }] }, isFetching: false });
      mockVersionDetail({ data: { config: {} } });
      // seedStoreAndRender([]) seeds data.l3 = {routes: []} (a valid-empty,
      // no-intent state, not an absent one); "changes nothing" means the
      // failed import leaves that exact value alone.
      seedStoreAndRender([]);

      fireEvent.click(screen.getByRole("button", { name: "Import from device config" }));
      await waitFor(() => expect(toastError).toHaveBeenCalledWith("Latest config version has no routes"));
      expect(storeRoutes()).toEqual([]);
    });

    it("an errored fetch toasts the error and changes nothing (review fix F3)", async () => {
      versionsQueryMock.mockReturnValue({ data: { items: [{ id: "cv-1" }] }, isFetching: false });
      mockVersionDetail({ isError: true, error: { response: { data: { detail: "boom" } } } });
      seedStoreAndRender([route({ interface: "eth0" })]);

      fireEvent.click(screen.getByRole("button", { name: "Import from device config" }));
      await waitFor(() => expect(toastError).toHaveBeenCalledWith("boom"));
      expect(storeRoutes()).toEqual([route({ interface: "eth0" })]);
    });
  });

  describe("problem rendering (review fixes F1, F4)", () => {
    it("renders a switch-level reason line at the top for a route:null entry", () => {
      seedStoreAndRender([route()], [problem({ reason: "l3_switch_unattached" })]);
      expect(screen.getByText("l3_switch_unattached")).toBeTruthy();
    });

    it("matches a per-row reason to the row with the same field values, not by original position", () => {
      // Row 1 (interface eth1) carries the problem; row 0 does not.
      seedStoreAndRender(
        [route({ interface: "eth0" }), route({ interface: "eth1" })],
        [problem({ route: route({ interface: "eth1" }), reason: "l3_unknown_interface" })],
      );
      expect(screen.getAllByText("l3_unknown_interface")).toHaveLength(1);
    });

    it("drops a problem whose route no longer matches any current row", () => {
      seedStoreAndRender(
        [route({ interface: "eth0" })],
        [problem({ route: route({ interface: "eth9" }), reason: "l3_unknown_interface" })],
      );
      expect(screen.queryByText("l3_unknown_interface")).toBeNull();
    });

    it("appends the detail message for l3_malformed-style switch-level reasons", () => {
      seedStoreAndRender(
        [route()],
        [problem({ reason: "l3_malformed", detail: "'l3' must be an object" })],
      );
      expect(screen.getByText("l3_malformed: 'l3' must be an object")).toBeTruthy();
    });

    it("renders l3_duplicate_route as an amber informational line, not red", () => {
      seedStoreAndRender(
        [route()],
        [problem({ route: route(), reason: "l3_duplicate_route" })],
      );
      const line = screen.getByText("Duplicate of another route on this switch");
      expect(line).toBeTruthy();
      expect(line.className).toContain("amber");
      expect(line.className).not.toContain("red");
      // The raw reason string itself is never shown for this reason.
      expect(screen.queryByText("l3_duplicate_route")).toBeNull();
    });

    it("each of two identically-valued rows gets its own matched problem (first-unmatched-row-wins)", () => {
      const dup = route({ interface: "eth0" });
      seedStoreAndRender(
        [dup, dup],
        [
          problem({ route: dup, reason: "l3_unknown_interface" }),
          problem({ route: dup, reason: "l3_bad_destination" }),
        ],
      );
      expect(screen.getByText("l3_unknown_interface")).toBeTruthy();
      expect(screen.getByText("l3_bad_destination")).toBeTruthy();
    });
  });

  // ADR 0014 addendum X-I (issue #755): a reason points at the box it is about.
  describe("per-field highlighting (addendum X-I)", () => {
    const invalid = (label: string) =>
      (screen.getByLabelText(label) as HTMLInputElement).className.includes("red");

    it("outlines the Virtual router box for l3_unknown_virtual_router", () => {
      const r = route({ virtual_router: "green" });
      seedStoreAndRender([r], [problem({ route: r, reason: "l3_unknown_virtual_router" })]);
      expect(invalid("Virtual router")).toBe(true);
      expect(invalid("Interface")).toBe(false);
      expect(invalid("Destination")).toBe(false);
    });

    it("outlines the Virtual router box for l3_interface_outside_virtual_router", () => {
      const r = route({ virtual_router: "blue", interface: "eth1" });
      seedStoreAndRender(
        [r],
        [problem({ route: r, reason: "l3_interface_outside_virtual_router" })],
      );
      expect(invalid("Virtual router")).toBe(true);
      expect(invalid("Interface")).toBe(false);
    });

    it("outlines the Interface box for l3_interface_bound_to_virtual_router", () => {
      // This reason fires on a route that names NO VRF: the problem is that its
      // interface is enslaved to one, so it points at Interface, not at the
      // (empty) Virtual router box.
      const r = route({ interface: "dummy0" });
      seedStoreAndRender(
        [r],
        [problem({ route: r, reason: "l3_interface_bound_to_virtual_router" })],
      );
      expect(invalid("Interface")).toBe(true);
      expect(invalid("Virtual router")).toBe(false);
    });

    it("outlines nothing for an informational duplicate", () => {
      const r = route();
      seedStoreAndRender([r], [problem({ route: r, reason: "l3_duplicate_route" })]);
      expect(invalid("Destination")).toBe(false);
      expect(invalid("Next hop")).toBe(false);
      expect(invalid("Interface")).toBe(false);
      expect(invalid("Virtual router")).toBe(false);
    });

    it("outlines nothing for a switch-level reason", () => {
      seedStoreAndRender([route()], [problem({ reason: "l3_switch_unattached" })]);
      expect(invalid("Destination")).toBe(false);
      expect(invalid("Virtual router")).toBe(false);
    });
  });

  describe("malformed data.l3 (review fix F6)", () => {
    it("shows a repair line and Remove all for {} rather than throwing", () => {
      const node = makeMalformedNode({});
      act(() => {
        useTopologyStore.setState({ nodes: [node], edges: [], selectedEdgeLayer: "L2" });
      });
      expect(() => render(<RoutingPanel node={node} problems={[]} />)).not.toThrow();
      expect(screen.getByText("Routing intent on this node is malformed")).toBeTruthy();
      expect(screen.getByRole("button", { name: "Remove all" })).toBeTruthy();
    });

    it("shows a repair line and Remove all for {routes: null} rather than throwing", () => {
      const node = makeMalformedNode({ routes: null });
      act(() => {
        useTopologyStore.setState({ nodes: [node], edges: [], selectedEdgeLayer: "L2" });
      });
      expect(() => render(<RoutingPanel node={node} problems={[]} />)).not.toThrow();
      expect(screen.getByText("Routing intent on this node is malformed")).toBeTruthy();
    });

    it("Remove all clears the malformed data.l3 entirely", () => {
      const node = makeMalformedNode({ routes: null });
      act(() => {
        useTopologyStore.setState({ nodes: [node], edges: [], selectedEdgeLayer: "L2" });
      });
      render(<RoutingPanel node={node} problems={[]} />);
      fireEvent.click(screen.getByRole("button", { name: "Remove all" }));
      const data = useTopologyStore.getState().nodes[0].data as DeviceNodeData;
      expect(data.l3).toBeUndefined();
    });

    it("{routes: []} counts as no intent, not malformed (the documented case)", () => {
      seedStoreAndRender([]);
      expect(screen.queryByText("Routing intent on this node is malformed")).toBeNull();
      expect(screen.getByText(/No routing intent/)).toBeTruthy();
    });
  });
});
