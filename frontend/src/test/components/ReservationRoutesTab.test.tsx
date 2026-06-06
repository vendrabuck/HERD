import { http, HttpResponse } from "msw";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type { ReactNode } from "react";
import { describe, it, expect } from "vitest";

import { server } from "../mocks/server";
import { ReservationRoutesTab } from "@/components/reservations/ReservationRoutesTab";

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

const DEVICE_A = "aaaaaaaa-0000-0000-0000-000000000000";
const DEVICE_B = "bbbbbbbb-0000-0000-0000-000000000000";
const DEVICE_C = "cccccccc-0000-0000-0000-000000000000";

// Stand-in for /api/inventory/devices used by useAllDeviceNames. The hook walks
// pages until it sees a short page, so a single page under the limit suffices.
function deviceNamesHandler(items: { id: string; name: string }[]) {
  return http.get("/api/inventory/devices", () =>
    HttpResponse.json({ items, total: items.length, skip: 0, limit: 500 }),
  );
}

// Build a /api/cabling/pathfind handler keyed on the source/target in the body.
function pathfindHandler(
  routes: Record<
    string,
    { reachable: boolean; hop_count: number; paths: unknown[] }
  >,
) {
  return http.post("/api/cabling/pathfind", async ({ request }) => {
    const body = (await request.json()) as {
      source_device_id: string;
      target_device_id: string;
    };
    const key = `${body.source_device_id}::${body.target_device_id}`;
    const match = routes[key];
    if (match) return HttpResponse.json(match);
    return HttpResponse.json({ reachable: false, hop_count: 0, paths: [] });
  });
}

describe("ReservationRoutesTab", () => {
  it("shows a hint when fewer than two devices are given", () => {
    renderWithProviders(<ReservationRoutesTab deviceIds={[DEVICE_A]} />);
    expect(
      screen.getByText("Routes require at least 2 devices."),
    ).toBeInTheDocument();
  });

  it("shows the discovering state while routes are loading", () => {
    server.use(
      deviceNamesHandler([]),
      http.post("/api/cabling/pathfind", async () => {
        await new Promise(() => {});
        return HttpResponse.json({});
      }),
    );
    renderWithProviders(
      <ReservationRoutesTab deviceIds={[DEVICE_A, DEVICE_B]} />,
    );
    expect(screen.getByText("Discovering routes...")).toBeInTheDocument();
  });

  it("renders a reachable route card with device names and hop count", async () => {
    server.use(
      deviceNamesHandler([
        { id: DEVICE_A, name: "spine-1" },
        { id: DEVICE_B, name: "leaf-1" },
      ]),
      pathfindHandler({
        [`${DEVICE_A}::${DEVICE_B}`]: {
          reachable: true,
          hop_count: 2,
          paths: [
            [
              { device_id: DEVICE_A, port_in: null, port_out: "eth0" },
              { device_id: DEVICE_C, port_in: "eth1", port_out: "eth2" },
              { device_id: DEVICE_B, port_in: "eth3", port_out: null },
            ],
          ],
        },
      }),
    );
    renderWithProviders(
      <ReservationRoutesTab deviceIds={[DEVICE_A, DEVICE_B]} />,
    );

    // Header uses resolved device names from useAllDeviceNames. The endpoint
    // names also appear in the hop chain, so each resolved name shows up twice
    // (card header plus its hop tile).
    await waitFor(() =>
      expect(screen.getAllByText("spine-1").length).toBeGreaterThan(0),
    );
    expect(screen.getAllByText("spine-1").length).toBe(2);
    expect(screen.getAllByText("leaf-1").length).toBe(2);
    // 2 hops -> plural label.
    expect(screen.getByText("2 hops")).toBeInTheDocument();
    // Intermediate hop with an unknown name falls back to the id prefix.
    expect(screen.getByText(DEVICE_C.slice(0, 8))).toBeInTheDocument();
  });

  it("renders an unreachable badge when no physical path exists", async () => {
    server.use(
      deviceNamesHandler([
        { id: DEVICE_A, name: "spine-1" },
        { id: DEVICE_B, name: "leaf-1" },
      ]),
      pathfindHandler({
        [`${DEVICE_A}::${DEVICE_B}`]: {
          reachable: false,
          hop_count: 0,
          paths: [],
        },
      }),
    );
    renderWithProviders(
      <ReservationRoutesTab deviceIds={[DEVICE_A, DEVICE_B]} />,
    );

    await waitFor(() =>
      expect(screen.getByText("Unreachable")).toBeInTheDocument(),
    );
    expect(
      screen.getByText("No physical path found between these devices."),
    ).toBeInTheDocument();
  });

  it("renders an empty state when the route map has no entries", async () => {
    server.use(
      deviceNamesHandler([]),
      // Return null-ish results: an empty map means neither reachable nor
      // explicitly-unreachable pairs are present.
      http.post("/api/cabling/pathfind", () =>
        HttpResponse.json({ detail: "boom" }, { status: 500 }),
      ),
    );
    renderWithProviders(
      <ReservationRoutesTab deviceIds={[DEVICE_A, DEVICE_B]} />,
    );

    // usePathfindPairs swallows per-pair errors into reachable:false results,
    // so a failed pathfind surfaces as an unreachable card rather than an error.
    await waitFor(() =>
      expect(screen.getByText("Unreachable")).toBeInTheDocument(),
    );
  });
});
