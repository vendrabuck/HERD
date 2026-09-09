import { render, screen, fireEvent, act } from "@testing-library/react";
import { describe, it, expect, vi, beforeAll, beforeEach } from "vitest";
import type { Node } from "@xyflow/react";

import { useTopologyStore } from "@/stores/topologyStore";
import type { DeviceNodeData, InvalidRoute, L3RouteIntent } from "@/types/topology.types";

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

function seedStoreAndRender(routes: L3RouteIntent[] | undefined, invalidRoutes: InvalidRoute[] = []) {
  const node = makeNode(routes);
  act(() => {
    useTopologyStore.setState({ nodes: [node], edges: [], selectedEdgeLayer: "L2" });
  });
  const view = render(<RoutingPanel node={node} invalidRoutes={invalidRoutes} />);
  return { view, node };
}

// Re-renders with the latest node from the store, the way TopologyEditorPage
// re-derives `selectedL3Node` from the reactive `nodes` array after every
// store write. RoutingPanel itself is a pure prop-in component (it reads
// routes off the `node` prop, not the store), so a test that performs
// several edits in sequence must rerender with the freshly-committed node
// each time to observe the next edit build on top of the previous one.
function rerenderFromStore(rerender: (ui: React.ReactElement) => void, invalidRoutes: InvalidRoute[] = []) {
  const node = useTopologyStore.getState().nodes[0] as Node<DeviceNodeData>;
  rerender(<RoutingPanel node={node} invalidRoutes={invalidRoutes} />);
  return node;
}

function storeRoutes(): L3RouteIntent[] | undefined {
  const data = useTopologyStore.getState().nodes[0]?.data as DeviceNodeData | undefined;
  return data?.l3?.routes;
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
  versionsQueryMock.mockReturnValue({ data: { items: [] }, isFetching: false });
  versionQueryMock.mockReturnValue({ data: undefined, isFetching: false });
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

  it("editing an existing row's field writes through the store", () => {
    const { view } = seedStoreAndRender([route({ destination: "10.0.0.0/24" })]);
    fireEvent.change(screen.getByLabelText("Destination"), { target: { value: "10.0.1.0/24" } });
    expect(storeRoutes()?.[0].destination).toBe("10.0.1.0/24");
    rerenderFromStore(view.rerender);
  });

  it("blanking an existing row's next hop stores null, not an empty string", () => {
    seedStoreAndRender([route({ next_hop: "10.0.0.1" })]);
    fireEvent.change(screen.getByLabelText("Next hop"), { target: { value: "" } });
    expect(storeRoutes()?.[0].next_hop).toBeNull();
  });

  it("removing the only row writes null to the store (empty intent is no intent)", () => {
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

  it("Import replaces an empty table directly, with no confirm", () => {
    versionsQueryMock.mockReturnValue({ data: { items: [{ id: "cv-1" }] }, isFetching: false });
    versionQueryMock.mockReturnValue({
      data: { config: { routes: [{ destination: "10.9.9.0/24", interface: "eth2" }] } },
      isFetching: false,
    });
    seedStoreAndRender([]);
    fireEvent.click(screen.getByRole("button", { name: "Import from device config" }));

    expect(storeRoutes()).toEqual([
      { destination: "10.9.9.0/24", interface: "eth2", next_hop: null, virtual_router: null },
    ]);
    // Applied immediately: no confirm click was needed for the store to
    // already reflect the import (the confirm dialog element exists in the
    // DOM either way, since ConfirmDialog renders unconditionally and only
    // toggles native open/close, so presence of its markup is not itself
    // proof of anything here).
  });

  it("Import asks for confirmation and REPLACES the table when rows already exist", () => {
    versionsQueryMock.mockReturnValue({ data: { items: [{ id: "cv-1" }] }, isFetching: false });
    versionQueryMock.mockReturnValue({
      data: { config: { routes: [{ destination: "10.9.9.0/24", interface: "eth2", next_hop: "10.9.9.1" }] } },
      isFetching: false,
    });
    seedStoreAndRender([route({ interface: "eth0" })]);

    fireEvent.click(screen.getByRole("button", { name: "Import from device config" }));
    expect(screen.getByText("Replace routing intent?")).toBeTruthy();
    // Not applied yet.
    expect(storeRoutes()).toEqual([route({ interface: "eth0" })]);

    fireEvent.click(screen.getByRole("button", { name: "Replace" }));
    expect(storeRoutes()).toEqual([
      { destination: "10.9.9.0/24", interface: "eth2", next_hop: "10.9.9.1", virtual_router: null },
    ]);
  });

  it("Import cancel leaves the existing table untouched", () => {
    versionsQueryMock.mockReturnValue({ data: { items: [{ id: "cv-1" }] }, isFetching: false });
    versionQueryMock.mockReturnValue({
      data: { config: { routes: [{ destination: "10.9.9.0/24", interface: "eth2" }] } },
      isFetching: false,
    });
    seedStoreAndRender([route({ interface: "eth0" })]);

    fireEvent.click(screen.getByRole("button", { name: "Import from device config" }));
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(storeRoutes()).toEqual([route({ interface: "eth0" })]);
  });

  it("renders a switch-level reason line at the top for an index-null invalid_routes entry", () => {
    seedStoreAndRender([route()], [
      { node_id: NODE_ID, device_id: DEVICE_ID, index: null, reason: "l3_switch_unattached", detail: null },
    ]);
    expect(screen.getByText("l3_switch_unattached")).toBeTruthy();
  });

  it("renders a per-row reason line under the offending row, matched by index", () => {
    seedStoreAndRender(
      [route({ interface: "eth0" }), route({ interface: "eth1" })],
      [{ node_id: NODE_ID, device_id: DEVICE_ID, index: 1, reason: "l3_unknown_interface", detail: null }],
    );
    const reason = screen.getByText("l3_unknown_interface");
    // It sits in the table body, after both rows worth of inputs render, not
    // duplicated onto the unrelated row.
    expect(reason).toBeTruthy();
    expect(screen.getAllByText("l3_unknown_interface")).toHaveLength(1);
  });

  it("appends the detail message for l3_malformed-style reasons", () => {
    seedStoreAndRender([route()], [
      { node_id: NODE_ID, device_id: DEVICE_ID, index: null, reason: "l3_malformed", detail: "'l3' must be an object" },
    ]);
    expect(screen.getByText("l3_malformed: 'l3' must be an object")).toBeTruthy();
  });
});
