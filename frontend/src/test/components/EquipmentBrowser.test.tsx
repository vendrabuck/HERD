import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { describe, it, expect } from "vitest";
import { http, HttpResponse } from "msw";

import { server } from "../mocks/server";
import { EquipmentBrowser } from "@/components/equipment-browser/EquipmentBrowser";
import type { Device } from "@/types/device.types";

// EquipmentBrowser is data-driven through two TanStack Query hooks:
// useTemplates("device") -> GET /api/inventory/templates
// useDevices({...})       -> GET /api/inventory/devices
// Both unwrap a paginated { items, total } envelope, so the MSW handlers
// below return that shape. We drive the component end to end through MSW
// rather than mocking the hooks, which exercises the real query wiring.

function makeDevice(overrides: Partial<Device>): Device {
  return {
    id: "dev-1",
    name: "device-1",
    template_id: "tmpl-1",
    template_name: "EX2200",
    template_icon: null,
    template_vendor: null,
    template_model: null,
    template_part_number: null,
    topology_type: "PHYSICAL",
    status: "AVAILABLE",
    field_data: {},
    exclusive: false,
    driver_id: null,
    driver_name: null,
    connection_type: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    created_by: null,
    created_by_name: null,
    modified_by: null,
    modified_by_name: null,
    poll_interval_seconds: null,
    resolved_poll_interval_seconds: null,
    ...overrides,
  };
}

function paginate<T>(items: T[]) {
  return { items, total: items.length, skip: 0, limit: 500 };
}

// Register the templates handler with an empty list by default so individual
// tests only need to override the devices endpoint they care about.
function stubTemplates(items: { id: string; name: string }[] = []) {
  server.use(
    http.get("/api/inventory/templates", () => HttpResponse.json(paginate(items))),
  );
}

function stubDevices(devices: Device[]) {
  server.use(
    http.get("/api/inventory/devices", () => HttpResponse.json(paginate(devices))),
  );
}

function renderWithProviders(ui: ReactNode) {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

describe("EquipmentBrowser", () => {
  it("renders the header, search input, and filter controls", async () => {
    stubTemplates([{ id: "tmpl-1", name: "EX2200" }]);
    stubDevices([]);

    renderWithProviders(<EquipmentBrowser />);

    expect(screen.getByText("Equipment Browser")).toBeInTheDocument();
    expect(screen.getByPlaceholderText("Search devices...")).toBeInTheDocument();
    expect(screen.getByLabelText("Template filter")).toBeInTheDocument();
    expect(screen.getByLabelText("Topology type filter")).toBeInTheDocument();

    // Template options are populated from useTemplates once the query resolves.
    await waitFor(() =>
      expect(
        within(screen.getByLabelText("Template filter")).getByRole("option", {
          name: "EX2200",
        }),
      ).toBeInTheDocument(),
    );
  });

  it("shows the inventory-empty hint when nothing is seeded and no filter is active", async () => {
    stubTemplates();
    stubDevices([]);

    renderWithProviders(<EquipmentBrowser />);

    expect(
      await screen.findByText(/No devices in inventory\. Ask an admin to add devices/),
    ).toBeInTheDocument();
    // The generic "filter matched nothing" copy must NOT show in the empty case.
    expect(screen.queryByText("No devices found")).not.toBeInTheDocument();
  });

  it("shows 'No devices found' when a search filter matches nothing", async () => {
    stubTemplates();
    stubDevices([]);

    renderWithProviders(<EquipmentBrowser />);

    // Inventory-empty + no filter shows the hint first.
    await screen.findByText(/No devices in inventory/);

    // Typing a search term makes a filter active; an empty result now reads as
    // "filter matched nothing", not "inventory empty".
    await userEvent.type(screen.getByPlaceholderText("Search devices..."), "nomatch");

    expect(await screen.findByText("No devices found")).toBeInTheDocument();
    expect(screen.queryByText(/No devices in inventory/)).not.toBeInTheDocument();
  });

  it("shows an error message when the devices request fails", async () => {
    stubTemplates();
    server.use(
      http.get("/api/inventory/devices", () =>
        HttpResponse.json({ detail: "boom" }, { status: 500 }),
      ),
    );

    renderWithProviders(<EquipmentBrowser />);

    expect(await screen.findByText("Failed to load devices")).toBeInTheDocument();
  });

  it("renders a device card per returned device with its topology badge", async () => {
    stubTemplates();
    stubDevices([
      makeDevice({ id: "d1", name: "phys-fw", topology_type: "PHYSICAL" }),
      makeDevice({ id: "d2", name: "cloud-fw", topology_type: "CLOUD" }),
    ]);

    renderWithProviders(<EquipmentBrowser />);

    expect(await screen.findByText("phys-fw")).toBeInTheDocument();
    expect(screen.getByText("cloud-fw")).toBeInTheDocument();
    expect(screen.getByText("PHY")).toBeInTheDocument();
    expect(screen.getByText("CLD")).toBeInTheDocument();
  });

  it("excludes devices already placed on the canvas", async () => {
    stubTemplates();
    stubDevices([
      makeDevice({ id: "on-canvas", name: "already-placed" }),
      makeDevice({ id: "free", name: "still-free" }),
    ]);

    renderWithProviders(<EquipmentBrowser canvasDeviceIds={["on-canvas"]} />);

    expect(await screen.findByText("still-free")).toBeInTheDocument();
    expect(screen.queryByText("already-placed")).not.toBeInTheDocument();
  });

  it("hides exclusive reserved devices when the show-reserved toggle is off", async () => {
    stubTemplates();
    stubDevices([
      makeDevice({
        id: "res",
        name: "reserved-exclusive",
        exclusive: true,
        status: "RESERVED",
      }),
      makeDevice({ id: "avail", name: "available-one", status: "AVAILABLE" }),
    ]);

    renderWithProviders(<EquipmentBrowser />);

    // Default: toggle on, both devices visible.
    expect(await screen.findByText("reserved-exclusive")).toBeInTheDocument();
    expect(screen.getByText("available-one")).toBeInTheDocument();

    // Turning the toggle off filters out the exclusive reserved device only.
    await userEvent.click(screen.getByRole("checkbox"));

    await waitFor(() =>
      expect(screen.queryByText("reserved-exclusive")).not.toBeInTheDocument(),
    );
    expect(screen.getByText("available-one")).toBeInTheDocument();
  });
});
