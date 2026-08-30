import { http, HttpResponse } from "msw";
import { render, screen, waitFor, fireEvent, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type { ReactNode } from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";

const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }));
vi.mock("react-hot-toast", () => ({ default: toast }));

const navigateSpy = vi.fn();
vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual<typeof import("react-router-dom")>("react-router-dom");
  return { ...actual, useNavigate: () => navigateSpy };
});

import { server } from "../mocks/server";
import { DeviceGroupsPage } from "@/pages/admin/DeviceGroupsPage";

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

const DEVICE_GROUPS = [
  {
    id: "dg-1",
    name: "Edge Firewalls",
    description: "Perimeter",
    device_count: 3,
    user_group_count: 2,
    created_by: null,
    created_at: "2026-01-05T00:00:00Z",
  },
  {
    id: "dg-2",
    name: "Lab Switches",
    description: null,
    device_count: 0,
    user_group_count: 0,
    created_by: null,
    created_at: "2026-02-10T00:00:00Z",
  },
];

function deviceGroupsHandler(items: typeof DEVICE_GROUPS, total = items.length) {
  return http.get("/api/inventory/device-groups", () =>
    HttpResponse.json({ items, total, skip: 0, limit: 50 }),
  );
}

beforeEach(() => {
  navigateSpy.mockClear();
  toast.success.mockClear();
  toast.error.mockClear();
});

describe("DeviceGroupsPage", () => {
  it("shows the loading state before device groups resolve", () => {
    server.use(
      http.get("/api/inventory/device-groups", async () => {
        await new Promise(() => {});
        return HttpResponse.json({});
      }),
    );
    renderWithProviders(<DeviceGroupsPage />);
    expect(screen.getByText("Loading device groups...")).toBeInTheDocument();
  });

  it("renders an empty state when there are no device groups", async () => {
    server.use(deviceGroupsHandler([]));
    renderWithProviders(<DeviceGroupsPage />);
    await waitFor(() =>
      expect(screen.getByText("No device groups found")).toBeInTheDocument(),
    );
  });

  it("renders device group rows with counts and description fallback", async () => {
    server.use(deviceGroupsHandler(DEVICE_GROUPS));
    renderWithProviders(<DeviceGroupsPage />);

    const table = await screen.findByRole("table");
    const rows = within(table);
    await waitFor(() => expect(rows.getByText("Edge Firewalls")).toBeInTheDocument());
    expect(rows.getByText("Perimeter")).toBeInTheDocument();
    expect(rows.getByText("3")).toBeInTheDocument();
    expect(rows.getByText("2")).toBeInTheDocument();
    expect(rows.getByText("Lab Switches")).toBeInTheDocument();
    expect(rows.getByText("-")).toBeInTheDocument();
  });

  it("navigates to the create-device-group route when the create button is clicked", () => {
    server.use(deviceGroupsHandler([]));
    renderWithProviders(<DeviceGroupsPage />);
    fireEvent.click(screen.getByRole("button", { name: "Create Device Group" }));
    expect(navigateSpy).toHaveBeenCalledWith("/admin/device-groups/new");
  });

  it("navigates to the device group detail route when a row is clicked", async () => {
    server.use(deviceGroupsHandler(DEVICE_GROUPS));
    renderWithProviders(<DeviceGroupsPage />);
    const row = await screen.findByText("Edge Firewalls");
    fireEvent.click(row);
    expect(navigateSpy).toHaveBeenCalledWith("/admin/device-groups/dg-1");
  });

  it("deletes a device group through the confirm dialog", async () => {
    let deleteCalled = false;
    server.use(
      deviceGroupsHandler(DEVICE_GROUPS),
      http.delete("/api/inventory/device-groups/dg-1", () => {
        deleteCalled = true;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    renderWithProviders(<DeviceGroupsPage />);
    await screen.findByText("Edge Firewalls");

    fireEvent.click(screen.getAllByRole("button", { name: "Delete" })[0]);
    const dialog = within(screen.getByRole("dialog", { name: "Delete Device Group" }));
    expect(
      dialog.getByText(/All device and permission assignments will be removed/i),
    ).toBeInTheDocument();
    fireEvent.click(dialog.getByRole("button", { name: "Delete" }));

    await waitFor(() => expect(deleteCalled).toBe(true));
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith("Device group deleted"));
  });

  it("cancelling the delete confirm dialog does not call delete", async () => {
    let deleteCalled = false;
    server.use(
      deviceGroupsHandler(DEVICE_GROUPS),
      http.delete("/api/inventory/device-groups/dg-1", () => {
        deleteCalled = true;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    renderWithProviders(<DeviceGroupsPage />);
    await screen.findByText("Edge Firewalls");

    fireEvent.click(screen.getAllByRole("button", { name: "Delete" })[0]);
    const dialog = within(screen.getByRole("dialog", { name: "Delete Device Group" }));
    fireEvent.click(dialog.getByRole("button", { name: "Cancel" }));

    expect(deleteCalled).toBe(false);
  });

  it("surfaces the server detail message when delete fails", async () => {
    server.use(
      deviceGroupsHandler(DEVICE_GROUPS),
      http.delete("/api/inventory/device-groups/dg-1", () =>
        HttpResponse.json({ detail: "group still has devices" }, { status: 409 }),
      ),
    );
    renderWithProviders(<DeviceGroupsPage />);
    await screen.findByText("Edge Firewalls");

    fireEvent.click(screen.getAllByRole("button", { name: "Delete" })[0]);
    const dialog = within(screen.getByRole("dialog", { name: "Delete Device Group" }));
    fireEvent.click(dialog.getByRole("button", { name: "Delete" }));

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith("group still has devices"),
    );
  });

  it("falls back to a generic message when delete fails with no detail", async () => {
    server.use(
      deviceGroupsHandler(DEVICE_GROUPS),
      http.delete("/api/inventory/device-groups/dg-1", () => new HttpResponse(null, { status: 500 })),
    );
    renderWithProviders(<DeviceGroupsPage />);
    await screen.findByText("Edge Firewalls");

    fireEvent.click(screen.getAllByRole("button", { name: "Delete" })[0]);
    const dialog = within(screen.getByRole("dialog", { name: "Delete Device Group" }));
    fireEvent.click(dialog.getByRole("button", { name: "Delete" }));

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith("Failed to delete device group"),
    );
  });

  it("pages forward through the device group list", async () => {
    let capturedSkip: string | null = null;
    server.use(
      http.get("/api/inventory/device-groups", ({ request }) => {
        const url = new URL(request.url);
        capturedSkip = url.searchParams.get("skip");
        return HttpResponse.json({ items: DEVICE_GROUPS, total: 120, skip: 0, limit: 50 });
      }),
    );
    renderWithProviders(<DeviceGroupsPage />);
    await screen.findByText("Edge Firewalls");

    expect(screen.getByText("Showing 1-50 of 120")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Next" }));

    await waitFor(() => expect(capturedSkip).toBe("50"));
  });
});
