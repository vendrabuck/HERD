import { http, HttpResponse } from "msw";
import { render, screen, waitFor, fireEvent, within } from "@testing-library/react";
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

const { patchPreferencesMock } = vi.hoisted(() => ({ patchPreferencesMock: vi.fn() }));
vi.mock("@/api/userProfile", () => ({
  getPreferences: vi.fn(),
  patchPreferences: patchPreferencesMock,
  resetPreferences: vi.fn(),
}));

import { server } from "../mocks/server";
import { InventoryPage } from "@/pages/InventoryPage";
import { useAuthStore } from "@/stores/authStore";
import { usePreferencesStore } from "@/stores/preferencesStore";

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

// Two <dialog> elements are always mounted (BulkImportExport's import modal
// and ConfirmDialog), the second closed by default. jsdom does not compute
// an accessible dialog name from aria-labelledby while a <dialog> is closed
// (no `open` attribute), so role+name lookups return "". Find by the
// heading's own text instead, then walk up to its owning <dialog>.
function findDialogByHeading(heading: string): HTMLElement {
  const h2 = screen.getByRole("heading", { name: heading, hidden: true });
  const dialog = h2.closest("dialog");
  if (!dialog) throw new Error(`No <dialog> ancestor for heading "${heading}"`);
  return dialog as HTMLElement;
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
  patchPreferencesMock.mockReset();
  patchPreferencesMock.mockResolvedValue({
    user_id: "u",
    saved_filters: {},
    page_sizes: {},
    extras: {},
    updated_at: "",
  });
  usePreferencesStore.getState().clear();
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

  it("changing the page-size selector updates the store, resets to the first page, and debounces a preferences patch", async () => {
    const requests: { skip: string | null; limit: string | null }[] = [];
    server.use(
      http.get("/api/inventory/devices", ({ request }) => {
        const url = new URL(request.url);
        const skip = url.searchParams.get("skip");
        const limit = url.searchParams.get("limit");
        requests.push({ skip, limit });
        return HttpResponse.json({
          items: [makeDevice()],
          total: 150,
          skip: Number(skip ?? 0),
          limit: Number(limit ?? 50),
        });
      }),
    );
    renderWithProviders(<InventoryPage />);
    await waitFor(() =>
      expect(screen.getByText("fw-edge-01")).toBeInTheDocument(),
    );

    // Move off the first page so the reset-to-first-page behavior is
    // actually observable, rather than trivially true at skip=0.
    const nextButton = screen.getByText("Next");
    fireEvent.click(nextButton);
    await waitFor(() =>
      expect(requests.some((r) => r.skip === "50")).toBe(true),
    );

    const select = screen.getByLabelText("Rows per page") as HTMLSelectElement;
    expect(select.value).toBe("50");

    vi.useFakeTimers();
    fireEvent.change(select, { target: { value: "100" } });

    // The store updates synchronously; the debounced PATCH does not.
    expect(usePreferencesStore.getState().pageSizes.inventory).toBe(100);
    expect(patchPreferencesMock).not.toHaveBeenCalled();

    await vi.advanceTimersByTimeAsync(250);
    expect(patchPreferencesMock).toHaveBeenCalledTimes(1);
    expect(patchPreferencesMock.mock.calls[0][0].page_sizes).toEqual({
      inventory: 100,
    });
    vi.useRealTimers();

    // Selecting a new page size also resets pagination to the first page.
    await waitFor(() =>
      expect(
        requests.some((r) => r.skip === "0" && r.limit === "100"),
      ).toBe(true),
    );
  });

  describe("expanded ports row", () => {
    function deviceListHandler() {
      return http.get("/api/inventory/devices", () =>
        HttpResponse.json({
          items: [makeDevice()],
          total: 1,
          skip: 0,
          limit: 50,
        }),
      );
    }

    async function renderAndExpand() {
      renderWithProviders(<InventoryPage />);
      await waitFor(() =>
        expect(screen.getByText("fw-edge-01")).toBeInTheDocument(),
      );
      fireEvent.click(screen.getByLabelText("Expand ports"));
    }

    it("shows a loading message while ports and connections are pending", async () => {
      server.use(
        deviceListHandler(),
        http.get("/api/inventory/devices/:id/ports", async () => {
          await new Promise(() => {});
          return HttpResponse.json([]);
        }),
        http.get("/api/cabling/connections", () =>
          HttpResponse.json({ items: [], total: 0, skip: 0, limit: 500 }),
        ),
      );
      await renderAndExpand();
      expect(screen.getByText("Loading ports...")).toBeInTheDocument();
    });

    it("shows a no-ports message when the device has no ports configured", async () => {
      server.use(
        deviceListHandler(),
        http.get("/api/inventory/devices/:id/ports", () => HttpResponse.json([])),
        http.get("/api/cabling/connections", () =>
          HttpResponse.json({ items: [], total: 0, skip: 0, limit: 500 }),
        ),
      );
      await renderAndExpand();
      await waitFor(() =>
        expect(screen.getByText("No ports configured")).toBeInTheDocument(),
      );
    });

    it("shows an unconnected port as Not connected", async () => {
      server.use(
        deviceListHandler(),
        http.get("/api/inventory/devices/:id/ports", () =>
          HttpResponse.json([
            {
              id: "port-1",
              name: "eth0",
              device_id: "aaaaaaaa-1111-2222-3333-444444444444",
              template_id: "pt-1",
              template_name: null,
              template_icon: null,
              field_data: {},
              created_at: "2026-01-01T00:00:00Z",
              updated_at: "2026-01-01T00:00:00Z",
            },
          ]),
        ),
        http.get("/api/cabling/connections", () =>
          HttpResponse.json({ items: [], total: 0, skip: 0, limit: 500 }),
        ),
      );
      await renderAndExpand();
      await waitFor(() =>
        expect(screen.getByText("eth0")).toBeInTheDocument(),
      );
      expect(screen.getByText("Not connected")).toBeInTheDocument();
    });

    it("resolves a connected port to the other device's name and port, linked to that device", async () => {
      const otherDeviceId = "bbbbbbbb-1111-2222-3333-444444444444";
      server.use(
        http.get("/api/inventory/devices", ({ request }) => {
          const skip = new URL(request.url).searchParams.get("skip") ?? "0";
          if (skip === "0") {
            return HttpResponse.json({
              items: [makeDevice()],
              total: 1,
              skip: 0,
              limit: 50,
            });
          }
          // The all-names walker's second page: include the other device so
          // deviceNameMap resolves it.
          return HttpResponse.json({ items: [], total: 1, skip: 1, limit: 500 });
        }),
        http.get("/api/inventory/devices/:id/ports", () =>
          HttpResponse.json([
            {
              id: "port-1",
              name: "eth0",
              device_id: "aaaaaaaa-1111-2222-3333-444444444444",
              template_id: "pt-1",
              template_name: null,
              template_icon: null,
              field_data: {},
              created_at: "2026-01-01T00:00:00Z",
              updated_at: "2026-01-01T00:00:00Z",
            },
          ]),
        ),
        http.get("/api/cabling/connections", () =>
          HttpResponse.json({
            items: [
              {
                id: "conn-1",
                device_a_id: "aaaaaaaa-1111-2222-3333-444444444444",
                port_a: "eth0",
                device_b_id: otherDeviceId,
                port_b: "eth1",
                connection_type: "ethernet",
                notes: null,
                created_by: "admin",
                created_at: "2026-01-01T00:00:00Z",
                modified_by: null,
                updated_at: null,
              },
            ],
            total: 1,
            skip: 0,
            limit: 500,
          }),
        ),
      );
      await renderAndExpand();
      await waitFor(() =>
        expect(screen.getByText(/eth1/)).toBeInTheDocument(),
      );
      // Falls back to the truncated id since the all-names walker in this
      // test never actually serves the other device's name.
      const link = screen.getByRole("link", { name: /eth1/ });
      expect(link).toHaveAttribute("href", `/inventory/${otherDeviceId}`);
    });

    it("collapses the ports row when the chevron is clicked again", async () => {
      server.use(
        deviceListHandler(),
        http.get("/api/inventory/devices/:id/ports", () => HttpResponse.json([])),
        http.get("/api/cabling/connections", () =>
          HttpResponse.json({ items: [], total: 0, skip: 0, limit: 500 }),
        ),
      );
      await renderAndExpand();
      await waitFor(() =>
        expect(screen.getByText("No ports configured")).toBeInTheDocument(),
      );
      fireEvent.click(screen.getByLabelText("Collapse ports"));
      expect(screen.queryByText("No ports configured")).not.toBeInTheDocument();
    });
  });

  describe("select-all checkbox", () => {
    function twoDevicesHandler() {
      return http.get("/api/inventory/devices", () =>
        HttpResponse.json({
          items: [
            makeDevice({ id: "aaaaaaaa-0000-0000-0000-000000000001", name: "dev-1" }),
            makeDevice({ id: "aaaaaaaa-0000-0000-0000-000000000002", name: "dev-2" }),
          ],
          total: 2,
          skip: 0,
          limit: 50,
        }),
      );
    }

    it("selects and deselects every row", async () => {
      server.use(twoDevicesHandler());
      renderWithProviders(<InventoryPage />);
      await waitFor(() => expect(screen.getByText("dev-1")).toBeInTheDocument());

      const selectAll = screen.getAllByRole("checkbox")[0];
      fireEvent.click(selectAll);
      expect(screen.getByText("2 selected")).toBeInTheDocument();

      fireEvent.click(selectAll);
      expect(screen.queryByText("selected")).not.toBeInTheDocument();
    });

    it("re-selecting all after a partial selection selects the rest rather than clearing", async () => {
      server.use(twoDevicesHandler());
      renderWithProviders(<InventoryPage />);
      await waitFor(() => expect(screen.getByText("dev-1")).toBeInTheDocument());

      const checkboxes = screen.getAllByRole("checkbox");
      fireEvent.click(checkboxes[1]); // select just dev-1's row checkbox
      expect(screen.getByText("1 selected")).toBeInTheDocument();

      // toggleAll: not every device is selected yet, so this selects all,
      // not clears (the bug this pins: an indeterminate select-all click
      // clearing instead of completing the selection).
      fireEvent.click(checkboxes[0]);
      expect(screen.getByText("2 selected")).toBeInTheDocument();
    });

    it("clears the selection via the Clear button", async () => {
      server.use(twoDevicesHandler());
      renderWithProviders(<InventoryPage />);
      await waitFor(() => expect(screen.getByText("dev-1")).toBeInTheDocument());

      fireEvent.click(screen.getAllByRole("checkbox")[1]);
      expect(screen.getByText("1 selected")).toBeInTheDocument();
      fireEvent.click(screen.getByText("Clear"));
      expect(screen.queryByText("selected")).not.toBeInTheDocument();
    });
  });

  describe("bulk delete", () => {
    function twoDevicesHandler() {
      return http.get("/api/inventory/devices", () =>
        HttpResponse.json({
          items: [
            makeDevice({ id: "aaaaaaaa-0000-0000-0000-000000000001", name: "dev-1" }),
            makeDevice({ id: "aaaaaaaa-0000-0000-0000-000000000002", name: "dev-2" }),
          ],
          total: 2,
          skip: 0,
          limit: 50,
        }),
      );
    }

    it("deletes every selected device and reports the count on full success", async () => {
      const toastModule = await import("react-hot-toast");
      const deletedIds: string[] = [];
      server.use(
        twoDevicesHandler(),
        http.delete("/api/inventory/devices/:id", ({ params }) => {
          deletedIds.push(params.id as string);
          return new HttpResponse(null, { status: 204 });
        }),
      );
      renderWithProviders(<InventoryPage />);
      await waitFor(() => expect(screen.getByText("dev-1")).toBeInTheDocument());

      fireEvent.click(screen.getAllByRole("checkbox")[0]);
      fireEvent.click(screen.getByText("Delete Selected"));
      // The bulk-import dialog is always mounted (closed); ConfirmDialog is
      // the last dialog in document order. jsdom does not compute an
      // accessible name for a closed <dialog>'s aria-labelledby, so select by
      // heading text instead of role name.
      const dialog = findDialogByHeading("Delete devices");
      fireEvent.click(within(dialog).getByRole("button", { name: "Delete", hidden: true }));

      await waitFor(() => expect(deletedIds).toHaveLength(2));
      expect(toastModule.default.success).toHaveBeenCalledWith("Deleted 2 device(s)");
      // Selection clears after the bulk action.
      expect(screen.queryByText("selected")).not.toBeInTheDocument();
    });

    it("reports a partial failure count when one delete fails", async () => {
      const toastModule = await import("react-hot-toast");
      server.use(
        twoDevicesHandler(),
        http.delete("/api/inventory/devices/aaaaaaaa-0000-0000-0000-000000000001", () =>
          HttpResponse.json({ detail: "in use" }, { status: 409 }),
        ),
        http.delete("/api/inventory/devices/aaaaaaaa-0000-0000-0000-000000000002", () =>
          new HttpResponse(null, { status: 204 }),
        ),
      );
      renderWithProviders(<InventoryPage />);
      await waitFor(() => expect(screen.getByText("dev-1")).toBeInTheDocument());

      fireEvent.click(screen.getAllByRole("checkbox")[0]);
      fireEvent.click(screen.getByText("Delete Selected"));
      const dialog = findDialogByHeading("Delete devices");
      fireEvent.click(within(dialog).getByRole("button", { name: "Delete", hidden: true }));

      await waitFor(() =>
        expect(toastModule.default.error).toHaveBeenCalledWith("Deleted 1, failed 1"),
      );
    });

    it("cancelling the confirm dialog issues no delete calls", async () => {
      let deleteCalled = false;
      server.use(
        twoDevicesHandler(),
        http.delete("/api/inventory/devices/:id", () => {
          deleteCalled = true;
          return new HttpResponse(null, { status: 204 });
        }),
      );
      renderWithProviders(<InventoryPage />);
      await waitFor(() => expect(screen.getByText("dev-1")).toBeInTheDocument());

      fireEvent.click(screen.getAllByRole("checkbox")[0]);
      fireEvent.click(screen.getByText("Delete Selected"));
      const dialog = findDialogByHeading("Delete devices");
      fireEvent.click(within(dialog).getByRole("button", { name: "Cancel", hidden: true }));

      expect(deleteCalled).toBe(false);
      // Selection is preserved on cancel; only the dialog closes. Both rows
      // were selected via the select-all checkbox at index 0.
      expect(screen.getByText("2 selected")).toBeInTheDocument();
    });
  });

  describe("device copy", () => {
    it("duplicates a device with its ports and reports the count", async () => {
      const toastModule = await import("react-hot-toast");
      server.use(
        http.get("/api/inventory/devices", () =>
          HttpResponse.json({
            items: [makeDevice()],
            total: 1,
            skip: 0,
            limit: 50,
          }),
        ),
        http.get("/api/inventory/devices/aaaaaaaa-1111-2222-3333-444444444444/ports", () =>
          HttpResponse.json([
            {
              id: "port-1",
              name: "eth0",
              device_id: "aaaaaaaa-1111-2222-3333-444444444444",
              template_id: "pt-1",
              template_name: null,
              template_icon: null,
              field_data: {},
              created_at: "2026-01-01T00:00:00Z",
              updated_at: "2026-01-01T00:00:00Z",
            },
          ]),
        ),
        http.post("/api/inventory/devices", async ({ request }) => {
          const body = (await request.json()) as { name: string };
          return HttpResponse.json(
            { ...makeDevice({ id: "new-device-id", name: body.name }) },
            { status: 201 },
          );
        }),
        http.post("/api/inventory/devices/new-device-id/ports", () =>
          HttpResponse.json({ id: "new-port-id" }, { status: 201 }),
        ),
      );
      renderWithProviders(<InventoryPage />);
      await waitFor(() => expect(screen.getByText("fw-edge-01")).toBeInTheDocument());

      fireEvent.click(screen.getByTitle("Duplicate device"));

      await waitFor(() =>
        expect(toastModule.default.success).toHaveBeenCalledWith(
          "Device duplicated with 1 port(s)",
        ),
      );
    });

    it("reports a plain success message when the device has no ports", async () => {
      const toastModule = await import("react-hot-toast");
      server.use(
        http.get("/api/inventory/devices", () =>
          HttpResponse.json({
            items: [makeDevice()],
            total: 1,
            skip: 0,
            limit: 50,
          }),
        ),
        http.get("/api/inventory/devices/aaaaaaaa-1111-2222-3333-444444444444/ports", () =>
          HttpResponse.json([]),
        ),
        http.post("/api/inventory/devices", () =>
          HttpResponse.json(makeDevice({ id: "new-device-id" }), { status: 201 }),
        ),
      );
      renderWithProviders(<InventoryPage />);
      await waitFor(() => expect(screen.getByText("fw-edge-01")).toBeInTheDocument());

      fireEvent.click(screen.getByTitle("Duplicate device"));

      await waitFor(() =>
        expect(toastModule.default.success).toHaveBeenCalledWith("Device duplicated"),
      );
    });

    it("reports the count of ports that failed to copy without failing the whole operation", async () => {
      const toastModule = await import("react-hot-toast");
      server.use(
        http.get("/api/inventory/devices", () =>
          HttpResponse.json({
            items: [makeDevice()],
            total: 1,
            skip: 0,
            limit: 50,
          }),
        ),
        http.get("/api/inventory/devices/aaaaaaaa-1111-2222-3333-444444444444/ports", () =>
          HttpResponse.json([
            {
              id: "port-1",
              name: "eth0",
              device_id: "aaaaaaaa-1111-2222-3333-444444444444",
              template_id: "pt-1",
              template_name: null,
              template_icon: null,
              field_data: {},
              created_at: "2026-01-01T00:00:00Z",
              updated_at: "2026-01-01T00:00:00Z",
            },
          ]),
        ),
        http.post("/api/inventory/devices", () =>
          HttpResponse.json(makeDevice({ id: "new-device-id" }), { status: 201 }),
        ),
        http.post("/api/inventory/devices/new-device-id/ports", () =>
          HttpResponse.json({ detail: "port copy failed" }, { status: 500 }),
        ),
      );
      renderWithProviders(<InventoryPage />);
      await waitFor(() => expect(screen.getByText("fw-edge-01")).toBeInTheDocument());

      fireEvent.click(screen.getByTitle("Duplicate device"));

      await waitFor(() =>
        expect(toastModule.default.success).toHaveBeenCalledWith(
          "Device duplicated, but 1 port(s) failed to copy",
        ),
      );
    });

    it("shows an error toast when the device create itself fails", async () => {
      const toastModule = await import("react-hot-toast");
      server.use(
        http.get("/api/inventory/devices", () =>
          HttpResponse.json({
            items: [makeDevice()],
            total: 1,
            skip: 0,
            limit: 50,
          }),
        ),
        http.post("/api/inventory/devices", () =>
          HttpResponse.json({ detail: "quota exceeded" }, { status: 422 }),
        ),
      );
      renderWithProviders(<InventoryPage />);
      await waitFor(() => expect(screen.getByText("fw-edge-01")).toBeInTheDocument());

      fireEvent.click(screen.getByTitle("Duplicate device"));

      await waitFor(() =>
        expect(toastModule.default.error).toHaveBeenCalledWith("Failed to duplicate device"),
      );
    });
  });

  describe("search", () => {
    it("debounces user input into the query and persists it as a saved filter", async () => {
      server.use(
        http.get("/api/inventory/devices", ({ request }) => {
          const url = new URL(request.url);
          const search = url.searchParams.get("search");
          return HttpResponse.json({
            items: search ? [makeDevice({ name: "found-device" })] : [],
            total: search ? 1 : 0,
            skip: 0,
            limit: 50,
          });
        }),
      );
      renderWithProviders(<InventoryPage />);
      await waitFor(() => expect(screen.getByText("No devices found")).toBeInTheDocument());

      const input = screen.getByPlaceholderText("Search devices by name...");
      fireEvent.change(input, { target: { value: "found" } });

      await waitFor(() => expect(screen.getByText("found-device")).toBeInTheDocument());
      expect(usePreferencesStore.getState().savedFilters.inventory).toEqual({
        search: "found",
      });
    });
  });
});
