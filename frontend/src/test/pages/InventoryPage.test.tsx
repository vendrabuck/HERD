import { http, HttpResponse } from "msw";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type { ReactNode } from "react";
import { describe, it, expect, vi, beforeAll, beforeEach } from "vitest";

beforeAll(() => {
  HTMLDialogElement.prototype.showModal = vi.fn();
  HTMLDialogElement.prototype.close = vi.fn();
});

// Toasts are fire-and-forget side effects; stub so bulk-delete and copy paths
// do not blow up and so we can assert on them if needed.
vi.mock("react-hot-toast", () => ({
  default: { success: vi.fn(), error: vi.fn() },
}));

import { server } from "../mocks/server";
import { InventoryPage } from "@/pages/InventoryPage";
import { useAuthStore } from "@/stores/authStore";

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

function makeDevice(overrides: Partial<Record<string, unknown>> = {}) {
  return {
    id: "aaaaaaaa-1111-2222-3333-444444444444",
    name: "fw-edge-01",
    template_id: "tmpl-1",
    template_name: "FW-3600",
    template_icon: null,
    template_vendor: "vendor",
    template_model: "FW-3600",
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

// useAllDeviceNames walks /inventory/devices with skip/limit too; default it to
// empty so tests that do not care about names do not hang on a second request.
function defaultDeviceNamesHandler() {
  return http.get("/api/inventory/devices", () =>
    HttpResponse.json({ items: [], total: 0, skip: 0, limit: 500 }),
  );
}

function setAuthRole(role: string | null) {
  useAuthStore.setState({
    user: role
      ? { id: "1", role, username: "admin", email: "a@b.c" }
      : null,
  } as never);
}

beforeEach(() => {
  setAuthRole("admin");
  server.use(defaultDeviceNamesHandler());
});

describe("InventoryPage", () => {
  it("shows the loading skeleton while the device list is pending", () => {
    server.use(
      http.get("/api/inventory/devices", async () => {
        await new Promise(() => {});
        return HttpResponse.json({});
      }),
    );
    renderWithProviders(<InventoryPage />);
    expect(screen.getByRole("status")).toBeInTheDocument();
  });

  it("renders an error state when the device list fetch fails", async () => {
    server.use(
      http.get("/api/inventory/devices", () =>
        HttpResponse.json({ detail: "boom" }, { status: 500 }),
      ),
    );
    renderWithProviders(<InventoryPage />);
    await waitFor(() =>
      expect(screen.getByText("Failed to load devices")).toBeInTheDocument(),
    );
  });

  it("renders an empty-row message when the page has no devices", async () => {
    server.use(
      http.get("/api/inventory/devices", () =>
        HttpResponse.json({ items: [], total: 0, skip: 0, limit: 50 }),
      ),
    );
    renderWithProviders(<InventoryPage />);
    await waitFor(() =>
      expect(screen.getByText("No devices found")).toBeInTheDocument(),
    );
  });

  it("renders a device row with name, template, status, and the total count", async () => {
    server.use(
      // This handler matches both the paginated list query and the all-names
      // walker (both hit /inventory/devices). One device is enough for the row.
      http.get("/api/inventory/devices", () =>
        HttpResponse.json({
          items: [makeDevice()],
          total: 1,
          skip: 0,
          limit: 50,
        }),
      ),
    );
    renderWithProviders(<InventoryPage />);
    await waitFor(() =>
      expect(screen.getByText("fw-edge-01")).toBeInTheDocument(),
    );
    expect(screen.getByText("FW-3600")).toBeInTheDocument();
    expect(screen.getByText("AVAILABLE")).toBeInTheDocument();
    // The count badge next to the "All Devices" heading.
    expect(screen.getByText("(1)")).toBeInTheDocument();
  });

  it("shows the bulk-action bar after an admin selects a device", async () => {
    server.use(
      http.get("/api/inventory/devices", () =>
        HttpResponse.json({
          items: [makeDevice()],
          total: 1,
          skip: 0,
          limit: 50,
        }),
      ),
    );
    renderWithProviders(<InventoryPage />);
    await waitFor(() =>
      expect(screen.getByText("fw-edge-01")).toBeInTheDocument(),
    );
    // No selection yet, so no bulk bar.
    expect(screen.queryByText("Delete Selected")).not.toBeInTheDocument();

    // The per-row select checkbox is the unchecked checkbox in the table body;
    // the header select-all is also a checkbox, so target the row one by index.
    const checkboxes = screen.getAllByRole("checkbox");
    // [0] = select-all header, [1] = the device row checkbox.
    fireEvent.click(checkboxes[1]);

    expect(screen.getByText("1 selected")).toBeInTheDocument();
    expect(screen.getByText("Delete Selected")).toBeInTheDocument();
  });

  it("hides admin-only controls for a non-admin user", async () => {
    setAuthRole("user");
    server.use(
      http.get("/api/inventory/devices", () =>
        HttpResponse.json({
          items: [makeDevice()],
          total: 1,
          skip: 0,
          limit: 50,
        }),
      ),
    );
    renderWithProviders(<InventoryPage />);
    await waitFor(() =>
      expect(screen.getByText("fw-edge-01")).toBeInTheDocument(),
    );
    // Non-admin: no Actions column header and no per-row selection checkboxes.
    expect(screen.queryByText("Actions")).not.toBeInTheDocument();
    expect(screen.queryAllByRole("checkbox")).toHaveLength(0);
  });
});
