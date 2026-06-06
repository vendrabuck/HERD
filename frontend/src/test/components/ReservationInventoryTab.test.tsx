import { http, HttpResponse } from "msw";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type { ReactNode } from "react";
import { describe, it, expect, beforeEach } from "vitest";

import { server } from "../mocks/server";
import { ReservationInventoryTab } from "@/components/reservations/ReservationInventoryTab";

function renderWithProviders(node: ReactNode) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>{node}</MemoryRouter>
    </QueryClientProvider>,
  );
}

const DEVICE_A = {
  id: "aaaaaaaa-0000-0000-0000-000000000001",
  name: "fw-edge-1",
  template_name: "FW-400",
  status: "RESERVED",
};
const DEVICE_B = {
  id: "bbbbbbbb-0000-0000-0000-000000000002",
  name: "sw-core-1",
  template_name: "Catalyst",
  status: "AVAILABLE",
};

// The all-device-names walker pages through GET /inventory/devices. A single
// short page (items.length < limit) terminates the walk, so one handler suffices.
function deviceListHandler(items: Array<Record<string, unknown>>) {
  return http.get("/api/inventory/devices", () =>
    HttpResponse.json({ items, total: items.length, skip: 0, limit: 500 }),
  );
}

beforeEach(() => {
  server.use(
    deviceListHandler([DEVICE_A, DEVICE_B]),
    http.get("/api/inventory/devices/:id", ({ params }) => {
      const id = params.id as string;
      const match = [DEVICE_A, DEVICE_B].find((d) => d.id === id);
      if (!match) return HttpResponse.json({ detail: "not found" }, { status: 404 });
      return HttpResponse.json(match);
    }),
    http.get("/api/inventory/devices/:id/ports", () => HttpResponse.json([])),
    http.get("/api/cabling/connections", () =>
      HttpResponse.json({ items: [], total: 0, skip: 0, limit: 500 }),
    ),
  );
});

describe("ReservationInventoryTab", () => {
  it("renders the empty state when there are no devices", () => {
    renderWithProviders(<ReservationInventoryTab deviceIds={[]} />);
    expect(
      screen.getByText("No devices in this reservation."),
    ).toBeInTheDocument();
    // The device-count header should not render in the empty case.
    expect(screen.queryByText(/^Devices \(/)).not.toBeInTheDocument();
  });

  it("renders a row per device with name, template, and status", async () => {
    renderWithProviders(
      <ReservationInventoryTab deviceIds={[DEVICE_A.id, DEVICE_B.id]} />,
    );

    expect(screen.getByText("Devices (2)")).toBeInTheDocument();
    expect(await screen.findByText("fw-edge-1")).toBeInTheDocument();
    expect(screen.getByText("sw-core-1")).toBeInTheDocument();
    expect(screen.getByText("FW-400")).toBeInTheDocument();
    expect(screen.getByText("RESERVED")).toBeInTheDocument();
    expect(screen.getByText("AVAILABLE")).toBeInTheDocument();
  });

  it("expanding a row loads its ports and shows the no-ports state", async () => {
    renderWithProviders(<ReservationInventoryTab deviceIds={[DEVICE_A.id]} />);

    const row = await screen.findByText("fw-edge-1");
    // The ports detail (and its no-ports message) only mounts once expanded.
    expect(screen.queryByText("No ports")).not.toBeInTheDocument();

    fireEvent.click(row);

    expect(await screen.findByText("No ports")).toBeInTheDocument();
  });

  it("expanding a row renders ports and links connected peers", async () => {
    const PEER_PORT = "eth1/1";
    server.use(
      http.get("/api/inventory/devices/:id/ports", () =>
        HttpResponse.json([
          { id: "p1", name: "eth1/1", device_id: DEVICE_A.id },
          { id: "p2", name: "eth1/2", device_id: DEVICE_A.id },
        ]),
      ),
      http.get("/api/cabling/connections", () =>
        HttpResponse.json({
          items: [
            {
              id: "c1",
              device_a_id: DEVICE_A.id,
              port_a: PEER_PORT,
              device_b_id: DEVICE_B.id,
              port_b: "eth5/5",
            },
          ],
          total: 1,
          skip: 0,
          limit: 500,
        }),
      ),
    );

    renderWithProviders(<ReservationInventoryTab deviceIds={[DEVICE_A.id]} />);
    const row = await screen.findByText("fw-edge-1");
    fireEvent.click(row);

    // Connected port resolves the peer device name from the all-names map and
    // links to its inventory detail page.
    const link = await screen.findByRole("link", { name: /sw-core-1, eth5\/5/ });
    expect(link).toHaveAttribute("href", `/inventory/${DEVICE_B.id}`);
    // The unconnected port (eth1/2) renders without a link.
    expect(screen.getByText("eth1/2")).toBeInTheDocument();
  });

  it("falls back to a truncated id when the device lookup is missing", async () => {
    const orphanId = "ffffffff-9999-9999-9999-999999999999";
    // Not present in the device list or detail endpoint, so both the name map
    // and the detail query miss; the row falls back to the truncated id.
    renderWithProviders(<ReservationInventoryTab deviceIds={[orphanId]} />);

    await waitFor(() =>
      expect(screen.getByText(`${orphanId.slice(0, 8)}...`)).toBeInTheDocument(),
    );
  });
});
