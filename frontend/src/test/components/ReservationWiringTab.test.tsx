import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import type { WiringConnectionStatus, WiringStatusResponse } from "@/types/reservation.types";

// The tab is a thin projection of the two wiring hooks plus the device-name map;
// mock all three so the test drives the rendering matrix directly.
const wiringStatusMock = vi.fn();
const retryMutate = vi.fn();
let retryPending = false;
vi.mock("@/api/reservations", () => ({
  useReservationWiringStatus: () => wiringStatusMock(),
  useRetryReservationWiring: () => ({ mutate: retryMutate, isPending: retryPending }),
}));
vi.mock("@/api/inventory", () => ({
  useAllDeviceNames: () => ({ data: new Map([["sw-1", "leaf-a"]]) }),
}));

import { ReservationWiringTab } from "@/components/reservations/ReservationWiringTab";

const RES_ID = "res-1";

function conn(overrides: Partial<WiringConnectionStatus> = {}): WiringConnectionStatus {
  return {
    id: "c-1",
    switch_device_id: "sw-1",
    layer: "l1",
    port_a: "et1",
    port_b: "et2",
    physical_connection_id: "p-1",
    status: "ACTIVE",
    intended: "ACTIVE",
    attempts: 1,
    last_error: null,
    retryable: false,
    created_at: "2026-07-01T00:00:00Z",
    released_at: null,
    ...overrides,
  };
}

function l2Conn(overrides: Partial<WiringConnectionStatus> = {}): WiringConnectionStatus {
  return conn({
    id: "m-1",
    layer: "l2",
    port_a: null,
    port_b: null,
    physical_connection_id: null,
    port: "p3",
    vlan_assignment_id: "va-1",
    vlan: 101,
    ...overrides,
  });
}

function l3Conn(overrides: Partial<WiringConnectionStatus> = {}): WiringConnectionStatus {
  return conn({
    id: "r-1",
    layer: "l3",
    port_a: null,
    port_b: null,
    physical_connection_id: null,
    route_count: 2,
    ...overrides,
  });
}

function status(overrides: Partial<WiringStatusResponse> = {}): WiringStatusResponse {
  return {
    reservation_id: RES_ID,
    last_applied_fork_version: 2,
    frozen: false,
    connections: [],
    ...overrides,
  };
}

function setStatus(resp: Partial<ReturnType<typeof wiringStatusMock>>) {
  wiringStatusMock.mockReturnValue({ data: undefined, isLoading: false, isError: false, ...resp });
}

beforeEach(() => {
  retryMutate.mockReset();
  retryPending = false;
  wiringStatusMock.mockReset();
});

describe("ReservationWiringTab", () => {
  it("shows a loading state", () => {
    setStatus({ isLoading: true });
    render(<ReservationWiringTab reservationId={RES_ID} active />);
    expect(screen.getByText(/Loading wiring status/i)).toBeInTheDocument();
  });

  it("shows an error state", () => {
    setStatus({ isError: true });
    render(<ReservationWiringTab reservationId={RES_ID} active />);
    expect(screen.getByText(/Failed to load wiring status/i)).toBeInTheDocument();
  });

  it("shows a clean empty state for a reservation with no wiring rows", () => {
    setStatus({ data: status({ connections: [], last_applied_fork_version: null }) });
    render(<ReservationWiringTab reservationId={RES_ID} active />);
    expect(screen.getByText(/No per-connection wiring/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Retry failed/i })).not.toBeInTheDocument();
  });

  it("renders rows with switch name, port pair, status, and the applied version", () => {
    setStatus({ data: status({ connections: [conn()] }) });
    render(<ReservationWiringTab reservationId={RES_ID} active />);
    expect(screen.getByText("leaf-a")).toBeInTheDocument();
    expect(screen.getByText("et1 to et2")).toBeInTheDocument();
    expect(screen.getByText("ACTIVE")).toBeInTheDocument();
    expect(screen.getByText("2")).toBeInTheDocument(); // applied fork version
  });

  it("ACTIVE reservation with a retryable FAILED row shows the Retry button and last_error", () => {
    setStatus({
      data: status({
        connections: [conn({ id: "c-f", status: "FAILED", retryable: true, last_error: "driver timeout", attempts: 3 })],
      }),
    });
    render(<ReservationWiringTab reservationId={RES_ID} active />);
    expect(screen.getByRole("button", { name: /Retry failed/i })).toBeInTheDocument();
    expect(screen.getByText("driver timeout")).toBeInTheDocument();
  });

  it("a not_retryable FAILED row shows the re-save recovery hint and no retry button", () => {
    setStatus({
      data: status({
        connections: [conn({ id: "c-nr", status: "FAILED", retryable: false, last_error: "recorded hop unresolvable", attempts: 1 })],
      }),
    });
    render(<ReservationWiringTab reservationId={RES_ID} active />);
    expect(screen.getByText(/re-save the fork wiring/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Retry failed/i })).not.toBeInTheDocument();
  });

  it("an ended reservation renders read-only (no retry button) even with a retryable FAILED row", () => {
    setStatus({
      data: status({
        connections: [conn({ id: "c-f", status: "FAILED", retryable: true, last_error: "driver timeout" })],
      }),
    });
    render(<ReservationWiringTab reservationId={RES_ID} active={false} />);
    expect(screen.queryByRole("button", { name: /Retry failed/i })).not.toBeInTheDocument();
    // The failure detail is still shown read-only.
    expect(screen.getByText("driver timeout")).toBeInTheDocument();
  });

  it("shows the frozen marker when the wiring state is frozen", () => {
    setStatus({ data: status({ frozen: true, connections: [conn()] }) });
    render(<ReservationWiringTab reservationId={RES_ID} active={false} />);
    expect(screen.getByText("Frozen")).toBeInTheDocument();
  });

  it("groups layered rows into L1/L2/L3 sections with layer-specific detail", () => {
    setStatus({
      data: status({
        connections: [conn(), l2Conn(), l3Conn()],
      }),
    });
    render(<ReservationWiringTab reservationId={RES_ID} active />);
    expect(screen.getByText("L1 cross-connects")).toBeInTheDocument();
    expect(screen.getByText("L2 VLAN memberships")).toBeInTheDocument();
    expect(screen.getByText("L3 route pins")).toBeInTheDocument();
    expect(screen.getByText("et1 to et2")).toBeInTheDocument();
    expect(screen.getByText("port p3 VLAN 101")).toBeInTheDocument();
    expect(screen.getByText("2 routes")).toBeInTheDocument();
  });

  it("renders only nonempty layer sections", () => {
    setStatus({ data: status({ connections: [conn()] }) });
    render(<ReservationWiringTab reservationId={RES_ID} active />);
    expect(screen.getByText("L1 cross-connects")).toBeInTheDocument();
    expect(screen.queryByText("L2 VLAN memberships")).not.toBeInTheDocument();
    expect(screen.queryByText("L3 route pins")).not.toBeInTheDocument();
  });

  it("treats a row without a layer tag as L1 (pre-phase-8 backend tolerance)", () => {
    setStatus({ data: status({ connections: [conn({ layer: undefined })] }) });
    render(<ReservationWiringTab reservationId={RES_ID} active />);
    expect(screen.getByText("L1 cross-connects")).toBeInTheDocument();
    expect(screen.getByText("et1 to et2")).toBeInTheDocument();
  });

  it("an L2 row with a parked allocation shows VLAN unassigned", () => {
    setStatus({
      data: status({
        connections: [
          l2Conn({ vlan: null, status: "FAILED", retryable: true, last_error: "no allocation" }),
        ],
      }),
    });
    render(<ReservationWiringTab reservationId={RES_ID} active />);
    expect(screen.getByText("port p3 VLAN unassigned")).toBeInTheDocument();
  });

  it("a retryable FAILED L2 membership row enables the Retry button", () => {
    setStatus({
      data: status({
        connections: [
          conn(),
          l2Conn({ status: "FAILED", retryable: true, last_error: "add_to_vlan boom" }),
        ],
      }),
    });
    render(<ReservationWiringTab reservationId={RES_ID} active />);
    expect(screen.getByRole("button", { name: /Retry failed/i })).toBeInTheDocument();
    expect(screen.getByText("add_to_vlan boom")).toBeInTheDocument();
    expect(screen.getByText("1 failed")).toBeInTheDocument();
  });

  it("a FAILED row intended RELEASED shows the release-pending marker", () => {
    setStatus({
      data: status({
        connections: [
          l3Conn({
            status: "FAILED",
            intended: "RELEASED",
            retryable: true,
            last_error: "remove_route boom",
          }),
        ],
      }),
    });
    render(<ReservationWiringTab reservationId={RES_ID} active={false} />);
    expect(screen.getByText("release pending")).toBeInTheDocument();
  });

  it("clicking Retry failed calls the retry mutation with the reservation id", () => {
    setStatus({
      data: status({
        connections: [conn({ id: "c-f", status: "FAILED", retryable: true, last_error: "driver timeout" })],
      }),
    });
    render(<ReservationWiringTab reservationId={RES_ID} active />);
    fireEvent.click(screen.getByRole("button", { name: /Retry failed/i }));
    expect(retryMutate).toHaveBeenCalledTimes(1);
    expect(retryMutate).toHaveBeenCalledWith(RES_ID);
  });

  it("disables the Retry button while a retry is pending", () => {
    retryPending = true;
    setStatus({
      data: status({
        connections: [conn({ id: "c-f", status: "FAILED", retryable: true })],
      }),
    });
    render(<ReservationWiringTab reservationId={RES_ID} active />);
    expect(screen.getByRole("button", { name: /Retrying/i })).toBeDisabled();
  });
});
