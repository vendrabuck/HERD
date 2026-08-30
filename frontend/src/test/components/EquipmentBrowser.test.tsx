import { render, screen, waitFor, within, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { describe, it, expect, vi } from "vitest";
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

// The browser now issues two template queries (template_type=device for the
// filter dropdown, template_type=dynamic for the dynamic section); this stub
// answers each with its own list.
function stubTemplatesByType(byType: Record<string, Array<Record<string, unknown>>>) {
  server.use(
    http.get("/api/inventory/templates", ({ request }) => {
      const type = new URL(request.url).searchParams.get("template_type") ?? "";
      return HttpResponse.json(paginate(byType[type] ?? []));
    }),
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

  it("renders the device's template icon image when one is set", async () => {
    stubTemplates();
    stubDevices([
      makeDevice({ id: "d1", name: "iconed", template_icon: "/icons/ex2200.svg" }),
    ]);

    renderWithProviders(<EquipmentBrowser />);

    const img = (await screen.findByAltText("EX2200")) as HTMLImageElement;
    expect(img.tagName).toBe("IMG");
    expect(img.src).toContain("/icons/ex2200.svg");
  });

  it("drags an available device card with the device payload and copy effect", async () => {
    stubTemplates();
    stubDevices([makeDevice({ id: "d1", name: "grabbable", status: "AVAILABLE" })]);

    renderWithProviders(<EquipmentBrowser />);

    const card = (await screen.findByText("grabbable")).closest("[draggable]") as HTMLElement;
    expect(card).toHaveAttribute("draggable", "true");

    const dataTransfer = { setData: vi.fn(), effectAllowed: "" };
    fireEvent.dragStart(card, { dataTransfer });

    expect(dataTransfer.setData).toHaveBeenCalledWith(
      "application/herd-device",
      expect.stringContaining('"id":"d1"'),
    );
    expect(dataTransfer.effectAllowed).toBe("copy");
  });

  it("refuses to start a drag for an unavailable device card", async () => {
    stubTemplates();
    stubDevices([
      makeDevice({ id: "d1", name: "unavailable-card", status: "RESERVED", exclusive: false }),
    ]);

    renderWithProviders(<EquipmentBrowser />);

    const card = (await screen.findByText("unavailable-card")).closest(
      "[draggable]",
    ) as HTMLElement;
    // Non-draggable in the DOM, and title explains why.
    expect(card).toHaveAttribute("draggable", "false");
    expect(card).toHaveAttribute("title", "Not available: RESERVED");

    const dataTransfer = { setData: vi.fn(), effectAllowed: "" };
    const preventDefault = vi.fn();
    fireEvent.dragStart(card, { dataTransfer, preventDefault });

    // The handler's early return means setData is never reached.
    expect(dataTransfer.setData).not.toHaveBeenCalled();
  });

  it("filters devices by template via the template select", async () => {
    stubTemplates([{ id: "tmpl-1", name: "EX2200" }]);
    server.use(
      http.get("/api/inventory/devices", ({ request }) => {
        const templateId = new URL(request.url).searchParams.get("template_id");
        const items =
          templateId === "tmpl-1"
            ? [makeDevice({ id: "d1", name: "matched-by-template" })]
            : [makeDevice({ id: "d2", name: "unfiltered" })];
        return HttpResponse.json(paginate(items));
      }),
    );

    renderWithProviders(<EquipmentBrowser />);

    await screen.findByText("unfiltered");

    await userEvent.selectOptions(screen.getByLabelText("Template filter"), "tmpl-1");

    expect(await screen.findByText("matched-by-template")).toBeInTheDocument();
    expect(screen.queryByText("unfiltered")).not.toBeInTheDocument();
  });

  it("filters devices by topology type via the topology select", async () => {
    stubTemplates();
    server.use(
      http.get("/api/inventory/devices", ({ request }) => {
        const topo = new URL(request.url).searchParams.get("topology_type");
        const items =
          topo === "CLOUD"
            ? [makeDevice({ id: "d1", name: "cloud-only", topology_type: "CLOUD" })]
            : [makeDevice({ id: "d2", name: "all-topos" })];
        return HttpResponse.json(paginate(items));
      }),
    );

    renderWithProviders(<EquipmentBrowser />);

    await screen.findByText("all-topos");

    await userEvent.selectOptions(screen.getByLabelText("Topology type filter"), "CLOUD");

    expect(await screen.findByText("cloud-only")).toBeInTheDocument();
    expect(screen.queryByText("all-topos")).not.toBeInTheDocument();
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

  it("renders dynamic templates in their own section as drag sources", async () => {
    stubTemplatesByType({
      device: [],
      dynamic: [{ id: "dt-1", name: "Ubuntu VM", icon: null, template_type: "dynamic" }],
    });
    stubDevices([]);

    renderWithProviders(<EquipmentBrowser />);

    expect(await screen.findByText("Dynamic templates (1)")).toBeInTheDocument();
    expect(screen.getByText("Ubuntu VM")).toBeInTheDocument();
    expect(screen.getByText("DYN")).toBeInTheDocument();

    // Dragging a template card stages the dynamic-template payload, a separate
    // MIME type from the device drag. Scoped to the card containing the
    // template's own name: "Drag onto canvas" titles both this card and the
    // always-present network-element cards below it (ADR 0012).
    const card = screen.getByText("Ubuntu VM").closest("[draggable]") as HTMLElement;
    const setData = vi.fn();
    fireEvent.dragStart(card, { dataTransfer: { setData, effectAllowed: "" } });
    expect(setData).toHaveBeenCalledWith(
      "application/herd-dynamic-template",
      JSON.stringify({ id: "dt-1", name: "Ubuntu VM", icon: null }),
    );
  });

  it("collapses and re-expands the dynamic templates section", async () => {
    stubTemplatesByType({
      device: [],
      dynamic: [{ id: "dt-1", name: "Ubuntu VM", icon: null, template_type: "dynamic" }],
    });
    stubDevices([]);

    renderWithProviders(<EquipmentBrowser />);

    const toggle = await screen.findByRole("button", { name: /Dynamic templates \(1\)/ });
    expect(screen.getByText("Ubuntu VM")).toBeInTheDocument();

    await userEvent.click(toggle);
    expect(screen.queryByText("Ubuntu VM")).not.toBeInTheDocument();

    await userEvent.click(toggle);
    expect(screen.getByText("Ubuntu VM")).toBeInTheDocument();
  });

  it("omits the dynamic templates section when no dynamic templates exist", async () => {
    stubTemplatesByType({ device: [{ id: "tmpl-1", name: "EX2200" }], dynamic: [] });
    stubDevices([makeDevice({})]);

    renderWithProviders(<EquipmentBrowser />);

    // Wait for the data to land, then assert the section is absent entirely.
    expect(await screen.findByText("device-1")).toBeInTheDocument();
    expect(screen.queryByText(/Dynamic templates/)).not.toBeInTheDocument();
  });

  describe("Network elements section (ADR 0012 Editing surface)", () => {
    it("renders unconditionally, with no fetch and no absent-when-empty case", async () => {
      // Unlike dynamic templates, the four types are static: no template
      // stub returns anything and the section still renders.
      stubTemplates();
      stubDevices([]);

      renderWithProviders(<EquipmentBrowser />);

      expect(await screen.findByText("Network elements")).toBeInTheDocument();
      expect(screen.getByText("VLAN segment")).toBeInTheDocument();
      expect(screen.getByText("Subnet")).toBeInTheDocument();
      expect(screen.getByText("External cloud")).toBeInTheDocument();
      expect(screen.getByText("Patch trunk")).toBeInTheDocument();
    });

    it("drags a card with the application/herd-network-element MIME carrying element_type and a default label", async () => {
      stubTemplates();
      stubDevices([]);

      renderWithProviders(<EquipmentBrowser />);
      await screen.findByText("Network elements");

      const card = screen.getByText("VLAN segment").closest("[draggable]") as HTMLElement;
      const setData = vi.fn();
      fireEvent.dragStart(card, { dataTransfer: { setData, effectAllowed: "" } });

      expect(setData).toHaveBeenCalledWith(
        "application/herd-network-element",
        JSON.stringify({ element_type: "vlan_segment", label: "VLAN segment" }),
      );
    });

    it("collapses and re-expands the network elements section", async () => {
      stubTemplates();
      stubDevices([]);

      renderWithProviders(<EquipmentBrowser />);
      const toggle = await screen.findByRole("button", { name: "Network elements" });
      expect(screen.getByText("VLAN segment")).toBeInTheDocument();

      await userEvent.click(toggle);
      expect(screen.queryByText("VLAN segment")).not.toBeInTheDocument();

      await userEvent.click(toggle);
      expect(screen.getByText("VLAN segment")).toBeInTheDocument();
    });
  });
});
