import { http, HttpResponse } from "msw";
import { act, render, screen, waitFor, fireEvent, within } from "@testing-library/react";
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
import { usePreferencesStore, _flushPendingPatchForTest } from "@/stores/preferencesStore";
import { getPreferences } from "@/api/userProfile";

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
  // These tests model a store whose preferences GET has resolved; a write
  // made before the load is held until it settles (issue #985).
  usePreferencesStore.setState({ loaded: true });
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
    // Scoped to the row: the Status filter also lists AVAILABLE as an option.
    const row = screen.getByText("fw-edge-01").closest("tr") as HTMLElement;
    expect(within(row).getByText("FW-3600")).toBeInTheDocument();
    expect(within(row).getByText("AVAILABLE")).toBeInTheDocument();
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

  describe("list changes and row state (issue #938)", () => {
    const idA = "aaaaaaaa-1111-2222-3333-444444444444";
    const idB = "bbbbbbbb-1111-2222-3333-444444444444";
    const idC = "cccccccc-1111-2222-3333-444444444444";

    // The list the server returns for each search term; the empty term is the
    // unfiltered first page.
    function listBySearch(table: Record<string, string[]>) {
      const devices: Record<string, ReturnType<typeof makeDevice>> = {
        [idA]: makeDevice({ id: idA, name: "dev-a" }),
        [idB]: makeDevice({ id: idB, name: "dev-b" }),
        [idC]: makeDevice({ id: idC, name: "dev-c" }),
      };
      server.use(
        http.get("/api/inventory/devices", ({ request }) => {
          const search = new URL(request.url).searchParams.get("search") ?? "";
          const ids = table[search] ?? [];
          return HttpResponse.json({
            items: ids.map((id) => devices[id]),
            total: ids.length,
            skip: 0,
            limit: 50,
          });
        }),
        http.get("/api/inventory/devices/:id/ports", () => HttpResponse.json([])),
        http.get("/api/cabling/connections", () =>
          HttpResponse.json({ items: [], total: 0, skip: 0, limit: 500 }),
        ),
      );
    }

    function search(term: string) {
      fireEvent.change(screen.getByPlaceholderText("Search devices by name..."), {
        target: { value: term },
      });
    }

    it("keeps an expanded row open when a list change still contains it", async () => {
      listBySearch({ "": [idA, idB], keep: [idA, idC] });
      renderWithProviders(<InventoryPage />);
      await waitFor(() => expect(screen.getByText("dev-a")).toBeInTheDocument());
      const rowA = screen.getByText("dev-a").closest("tr") as HTMLElement;
      fireEvent.click(within(rowA).getByLabelText("Expand ports"));
      await waitFor(() => expect(screen.getByText("No ports configured")).toBeInTheDocument());

      search("keep");
      await waitFor(() => expect(screen.getByText("dev-c")).toBeInTheDocument());

      // dev-a survived the list change and is still expanded; dev-c is not.
      expect(screen.queryByText("dev-b")).not.toBeInTheDocument();
      expect(screen.getByText("No ports configured")).toBeInTheDocument();
      expect(screen.getAllByLabelText("Collapse ports")).toHaveLength(1);
      expect(screen.getAllByLabelText("Expand ports")).toHaveLength(1);
    });

    it("drops an expanded row whose id left the list and does not resurrect it", async () => {
      listBySearch({ "": [idA, idB], only_b: [idB] });
      renderWithProviders(<InventoryPage />);
      await waitFor(() => expect(screen.getByText("dev-a")).toBeInTheDocument());
      const rowA = screen.getByText("dev-a").closest("tr") as HTMLElement;
      fireEvent.click(within(rowA).getByLabelText("Expand ports"));
      await waitFor(() => expect(screen.getByText("No ports configured")).toBeInTheDocument());

      search("only_b");
      await waitFor(() => expect(screen.queryByText("dev-a")).not.toBeInTheDocument());
      expect(screen.queryByText("No ports configured")).not.toBeInTheDocument();

      // dev-a returns with the unfiltered list: its old expansion is gone.
      search("");
      await waitFor(() => expect(screen.getByText("dev-a")).toBeInTheDocument());
      expect(screen.queryByLabelText("Collapse ports")).not.toBeInTheDocument();
      expect(screen.getAllByLabelText("Expand ports")).toHaveLength(2);
    });

    it("still clears the bulk selection on a list change while keeping the expansion", async () => {
      listBySearch({ "": [idA, idB], keep: [idA, idC] });
      renderWithProviders(<InventoryPage />);
      await waitFor(() => expect(screen.getByText("dev-a")).toBeInTheDocument());
      const rowA = screen.getByText("dev-a").closest("tr") as HTMLElement;
      fireEvent.click(within(rowA).getByLabelText("Expand ports"));
      fireEvent.click(within(rowA).getByRole("checkbox"));
      expect(screen.getByText("1 selected")).toBeInTheDocument();

      search("keep");
      await waitFor(() => expect(screen.getByText("dev-c")).toBeInTheDocument());

      expect(screen.queryByText("1 selected")).not.toBeInTheDocument();
      expect(screen.getAllByLabelText("Collapse ports")).toHaveLength(1);
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

    it("says remove the cables first when a bulk delete is refused as cabled (issue #940)", async () => {
      const toastModule = await import("react-hot-toast");
      server.use(
        twoDevicesHandler(),
        http.delete("/api/inventory/devices/aaaaaaaa-0000-0000-0000-000000000001", () =>
          HttpResponse.json(
            { detail: { error: "device_cabled", connection_count: 2, connection_ids: ["c1"] } },
            { status: 409 },
          ),
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
        expect(toastModule.default.error).toHaveBeenCalledWith(
          "Deleted 1, failed 1. 1 still cabled: remove their cables first",
        ),
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

  describe("column filters (issue #842)", () => {
    const tmplA = "11111111-aaaa-bbbb-cccc-000000000001";
    const tmplB = "11111111-aaaa-bbbb-cccc-000000000002";
    const idA = "aaaaaaaa-1111-2222-3333-444444444444";
    const idB = "bbbbbbbb-1111-2222-3333-444444444444";

    function makeTemplate(id: string, name: string, vendor = "", model = "") {
      return { id, name, template_type: "device", vendor, model };
    }

    // Records the query string of every list request (the all-names walker also
    // hits this route, with limit=500, so tests filter on limit=50).
    function setup(opts: { total?: number; items?: unknown[] } = {}) {
      const requests: URLSearchParams[] = [];
      server.use(
        http.get("/api/inventory/templates", () =>
          HttpResponse.json({
            items: [makeTemplate(tmplA, "Switch-A", "Acme", "S1"), makeTemplate(tmplB, "Router-B")],
            total: 2,
            skip: 0,
            limit: 500,
          }),
        ),
        http.get("/api/inventory/devices", ({ request }) => {
          const params = new URL(request.url).searchParams;
          if (params.get("limit") === "50") requests.push(params);
          return HttpResponse.json({
            items: opts.items ?? [makeDevice({ id: idA, name: "dev-a" })],
            total: opts.total ?? 1,
            skip: Number(params.get("skip") ?? 0),
            limit: 50,
          });
        }),
        http.get("/api/inventory/devices/:id/ports", () => HttpResponse.json([])),
        http.get("/api/cabling/connections", () =>
          HttpResponse.json({ items: [], total: 0, skip: 0, limit: 500 }),
        ),
      );
      return requests;
    }

    const last = (r: URLSearchParams[]) => r[r.length - 1];

    async function ready() {
      renderWithProviders(<InventoryPage />);
      await waitFor(() => expect(screen.getByText("dev-a")).toBeInTheDocument());
      await waitFor(() => expect(screen.getByRole("option", { name: "Switch-A (Acme S1)" })).toBeInTheDocument());
    }

    const pick = (label: string, value: string) =>
      fireEvent.change(screen.getByLabelText(label), { target: { value } });

    it("holds the search and the three filters in the Filters region, ahead of the table (issue #957)", async () => {
      setup();
      await ready();
      const region = screen.getByRole("region", { name: "Filters" });
      const panel = within(region);
      expect(panel.getByRole("textbox", { name: "Search devices" })).toBeInTheDocument();
      expect(panel.getByRole("combobox", { name: "Status" })).toBeInTheDocument();
      expect(panel.getByRole("combobox", { name: "Template" })).toBeInTheDocument();
      expect(panel.getByRole("combobox", { name: "Topology" })).toBeInTheDocument();
      const table = screen.getByRole("table");
      expect(region.compareDocumentPosition(table) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    });

    it("sends no filter parameter at All, and labels templates with vendor and model", async () => {
      const requests = setup();
      await ready();
      const p = requests[0];
      expect(p.has("status")).toBe(false);
      expect(p.has("template_id")).toBe(false);
      expect(p.has("topology_type")).toBe(false);
      expect(p.has("search")).toBe(false);
      expect(screen.getByRole("option", { name: "Router-B" })).toBeInTheDocument();
      expect(screen.queryByText("Clear filters")).not.toBeInTheDocument();
    });

    it("each filter sends its own parameter, and All removes it again", async () => {
      const requests = setup();
      await ready();

      pick("Status", "RESERVED");
      await waitFor(() => expect(last(requests).get("status")).toBe("RESERVED"));
      pick("Template", tmplA);
      await waitFor(() => expect(last(requests).get("template_id")).toBe(tmplA));
      pick("Topology", "CLOUD");
      await waitFor(() => expect(last(requests).get("topology_type")).toBe("CLOUD"));
      expect(last(requests).get("status")).toBe("RESERVED");

      pick("Status", "");
      await waitFor(() => expect(last(requests).has("status")).toBe(false));
      expect(last(requests).get("template_id")).toBe(tmplA);
      expect(last(requests).get("topology_type")).toBe("CLOUD");
    });

    it("composes the filters with the search in one request", async () => {
      const requests = setup();
      await ready();
      pick("Status", "AVAILABLE");
      pick("Template", tmplB);
      fireEvent.change(screen.getByPlaceholderText("Search devices by name..."), {
        target: { value: "edge" },
      });
      await waitFor(() => expect(last(requests).get("search")).toBe("edge"));
      expect(last(requests).get("status")).toBe("AVAILABLE");
      expect(last(requests).get("template_id")).toBe(tmplB);
    });

    it("resets to the first page on a filter change and shows the filtered total", async () => {
      const requests = setup({ total: 150 });
      await ready();
      fireEvent.click(screen.getByText("Next"));
      await waitFor(() => expect(last(requests).get("skip")).toBe("50"));

      pick("Status", "OFFLINE");
      await waitFor(() => expect(last(requests).get("status")).toBe("OFFLINE"));
      expect(last(requests).get("skip")).toBe("0");
      expect(screen.getByText("(150)")).toBeInTheDocument();
    });

    it("persists every field, merges the search write with the filters, and round-trips", async () => {
      setup();
      await ready();
      pick("Status", "MAINTENANCE");
      pick("Template", tmplA);
      pick("Topology", "PHYSICAL");
      fireEvent.change(screen.getByPlaceholderText("Search devices by name..."), {
        target: { value: "abc" },
      });
      await waitFor(() =>
        expect(usePreferencesStore.getState().savedFilters.inventory).toEqual({
          search: "abc",
          status: "MAINTENANCE",
          template_id: tmplA,
          topology_type: "PHYSICAL",
        }),
      );
    });

    it("a filter change made while a search is pending still ends with all fields saved", async () => {
      setup();
      await ready();
      fireEvent.change(screen.getByPlaceholderText("Search devices by name..."), {
        target: { value: "abc" },
      });
      pick("Status", "OFFLINE");
      await waitFor(() =>
        expect(usePreferencesStore.getState().savedFilters.inventory).toEqual({
          search: "abc",
          status: "OFFLINE",
        }),
      );
    });

    it("restores saved filters into the controls and the first request", async () => {
      usePreferencesStore.setState({
        savedFilters: {
          inventory: { search: "dev", status: "RESERVED", template_id: tmplB, topology_type: "CLOUD" },
        },
      });
      const requests = setup();
      renderWithProviders(<InventoryPage />);
      await waitFor(() => expect(screen.getByText("dev-a")).toBeInTheDocument());
      expect(requests).toHaveLength(1);
      expect(Object.fromEntries(requests[0])).toMatchObject({
        search: "dev",
        status: "RESERVED",
        template_id: tmplB,
        topology_type: "CLOUD",
      });
      expect((screen.getByLabelText("Status") as HTMLSelectElement).value).toBe("RESERVED");
      expect((screen.getByLabelText("Template") as HTMLSelectElement).value).toBe(tmplB);
      expect((screen.getByLabelText("Topology") as HTMLSelectElement).value).toBe("CLOUD");
    });

    it("an old saved { search } object keeps working", async () => {
      usePreferencesStore.setState({ savedFilters: { inventory: { search: "old" } } });
      const requests = setup();
      renderWithProviders(<InventoryPage />);
      await waitFor(() => expect(screen.getByText("dev-a")).toBeInTheDocument());
      expect(requests[0].get("search")).toBe("old");
      expect(requests[0].has("status")).toBe(false);
    });

    it("falls back to All for a stale saved status, topology, or template and never sends them", async () => {
      usePreferencesStore.setState({
        savedFilters: {
          inventory: {
            status: "DECOMMISSIONED",
            template_id: "99999999-0000-0000-0000-000000000000",
            topology_type: "HYBRID",
          },
        },
      });
      const requests = setup();
      await ready();
      for (const p of requests) {
        expect(p.has("status")).toBe(false);
        expect(p.has("template_id")).toBe(false);
        expect(p.has("topology_type")).toBe(false);
      }
      expect((screen.getByLabelText("Status") as HTMLSelectElement).value).toBe("");
      expect((screen.getByLabelText("Template") as HTMLSelectElement).value).toBe("");
      expect((screen.getByLabelText("Topology") as HTMLSelectElement).value).toBe("");
    });

    it("a stale saved template is dropped from the next write while other fields persist", async () => {
      usePreferencesStore.setState({
        savedFilters: { inventory: { template_id: "99999999-0000-0000-0000-000000000000" } },
      });
      setup();
      await ready();
      pick("Status", "AVAILABLE");
      await waitFor(() =>
        expect(usePreferencesStore.getState().savedFilters.inventory).toEqual({
          search: "",
          status: "AVAILABLE",
        }),
      );
    });

    it("Clear filters resets every control, the request, and the saved state", async () => {
      const requests = setup();
      await ready();
      pick("Status", "RESERVED");
      pick("Template", tmplA);
      fireEvent.change(screen.getByPlaceholderText("Search devices by name..."), {
        target: { value: "abc" },
      });
      await waitFor(() => expect(last(requests).get("search")).toBe("abc"));

      fireEvent.click(screen.getByRole("button", { name: "Clear filters" }));
      await waitFor(() => expect(last(requests).has("search")).toBe(false));
      expect(last(requests).has("status")).toBe(false);
      expect(last(requests).has("template_id")).toBe(false);
      expect((screen.getByPlaceholderText("Search devices by name...") as HTMLInputElement).value).toBe("");
      expect((screen.getByLabelText("Status") as HTMLSelectElement).value).toBe("");
      expect(usePreferencesStore.getState().savedFilters.inventory).toEqual({ search: "" });
      expect(screen.queryByRole("button", { name: "Clear filters" })).not.toBeInTheDocument();
    });

    it("shows a filtered-empty state with a working Clear control, not the no-devices state", async () => {
      server.use(
        http.get("/api/inventory/templates", () =>
          HttpResponse.json({ items: [], total: 0, skip: 0, limit: 500 }),
        ),
        http.get("/api/inventory/devices", ({ request }) => {
          const filtered = new URL(request.url).searchParams.has("status");
          return HttpResponse.json({
            items: filtered ? [] : [makeDevice({ id: idA, name: "dev-a" })],
            total: filtered ? 0 : 1,
            skip: 0,
            limit: 50,
          });
        }),
      );
      renderWithProviders(<InventoryPage />);
      await waitFor(() => expect(screen.getByText("dev-a")).toBeInTheDocument());
      pick("Status", "OFFLINE");
      await waitFor(() =>
        expect(screen.getByText(/No devices match the current filters/)).toBeInTheDocument(),
      );
      expect(screen.queryByText("No devices found")).not.toBeInTheDocument();

      const inRow = within(screen.getByText(/No devices match/).closest("td") as HTMLElement);
      fireEvent.click(inRow.getByRole("button", { name: "Clear filters" }));
      await waitFor(() => expect(screen.getByText("dev-a")).toBeInTheDocument());
    });

    // Issue #985: a filter change made while the preferences GET is still in
    // flight. The page builds its write from the unloaded defaults (an empty
    // search); the store must hold it and save it merged over the loaded value.
    it("a filter change made before the preferences load keeps the saved search (issue #985)", async () => {
      let resolveGet!: (value: unknown) => void;
      (getPreferences as unknown as ReturnType<typeof vi.fn>).mockReturnValueOnce(
        new Promise((res) => {
          resolveGet = res;
        }),
      );
      usePreferencesStore.getState().clear();
      const loading = usePreferencesStore.getState().load();
      const requests = setup();
      await ready();
      pick("Status", "OFFLINE");
      _flushPendingPatchForTest();
      expect(patchPreferencesMock).not.toHaveBeenCalled();

      await act(async () => {
        resolveGet({
          user_id: "u",
          saved_filters: { inventory: { search: "foo" } },
          page_sizes: {},
          extras: {},
          updated_at: "",
        });
        await loading;
      });
      await waitFor(() => expect(last(requests).get("search")).toBe("foo"));
      expect(last(requests).get("status")).toBe("OFFLINE");
      expect(
        (screen.getByPlaceholderText("Search devices by name...") as HTMLInputElement).value,
      ).toBe("foo");
      _flushPendingPatchForTest();
      expect(patchPreferencesMock).toHaveBeenCalledTimes(1);
      expect(patchPreferencesMock.mock.calls[0][0].saved_filters).toEqual({
        inventory: { search: "foo", status: "OFFLINE" },
      });
    });

    // Issue #982: the preferences arrive after the first list request, as on a
    // full page reload. A saved search that loads then must apply at once (no
    // 300 ms debounce), and no control touched inside what used to be the
    // debounce window may persist a search the user did not type.
    describe("a saved search that loads after mount (issue #982)", () => {
      const searchBox = () =>
        screen.getByPlaceholderText("Search devices by name...") as HTMLInputElement;
      const saved = () => usePreferencesStore.getState().savedFilters.inventory;
      const loadLate = (inventory: Record<string, unknown>) =>
        act(() => {
          usePreferencesStore.setState({ savedFilters: { inventory } });
        });

      it("a saved search that loads after mount applies at once and survives a filter change", async () => {
        const requests = setup();
        await ready();
        expect(last(requests).has("search")).toBe(false);
        loadLate({ search: "late" });
        // Changed at once, before any 300 ms debounce could run.
        pick("Status", "RESERVED");
        await waitFor(() => expect(last(requests).get("status")).toBe("RESERVED"));
        expect(last(requests).get("search")).toBe("late");
        expect(saved()).toEqual({ search: "late", status: "RESERVED" });
        expect(searchBox().value).toBe("late");
      });

      it.each([
        ["Template", tmplA, { template_id: tmplA }, "template_id"],
        ["Topology", "CLOUD", { topology_type: "CLOUD" }, "topology_type"],
      ])(
        "survives a %s change made at once",
        async (label, value, savedField, param) => {
          const requests = setup();
          await ready();
          loadLate({ search: "late" });
          pick(label, value);
          await waitFor(() => expect(last(requests).get(param)).toBe(value));
          expect(last(requests).get("search")).toBe("late");
          expect(saved()).toEqual({ search: "late", ...savedField });
          expect(searchBox().value).toBe("late");
        },
      );

      it("is sent with the very next list request, before any debounce could fire", async () => {
        const requests = setup();
        await ready();
        const before = requests.length;
        loadLate({ search: "late" });
        // Well under the 300 ms debounce: only an immediate apply can pass this.
        await waitFor(() => expect(requests.length).toBeGreaterThan(before), { timeout: 150 });
        expect(requests[before].get("search")).toBe("late");
      });

      it("writes nothing by itself: no debounce timer is armed on load", async () => {
        setup();
        await ready();
        loadLate({ search: "late", status: "RESERVED" });
        await act(async () => {
          await new Promise((r) => setTimeout(r, 450));
        });
        expect(saved()).toEqual({ search: "late", status: "RESERVED" });
        expect(patchPreferencesMock).not.toHaveBeenCalled();
      });

      it("Clear filters at once clears it, and nothing restores it afterwards", async () => {
        const requests = setup();
        await ready();
        loadLate({ search: "late", status: "RESERVED" });
        fireEvent.click(screen.getByRole("button", { name: "Clear filters" }));
        await waitFor(() => expect(last(requests).has("status")).toBe(false));
        expect(last(requests).has("search")).toBe(false);
        expect(saved()).toEqual({ search: "" });
        await act(async () => {
          await new Promise((r) => setTimeout(r, 450));
        });
        expect(saved()).toEqual({ search: "" });
        expect(last(requests).has("search")).toBe(false);
        expect(searchBox().value).toBe("");
      });

      it("typing after the load still debounces, then applies and persists the typed value", async () => {
        const requests = setup();
        await ready();
        loadLate({ search: "late", status: "RESERVED" });
        await waitFor(() => expect(last(requests).get("search")).toBe("late"));
        const count = requests.length;
        fireEvent.change(searchBox(), { target: { value: "typed" } });
        // Nothing is sent or saved for the keystroke itself.
        expect(requests.length).toBe(count);
        expect(saved()).toEqual({ search: "late", status: "RESERVED" });
        await waitFor(() => expect(last(requests).get("search")).toBe("typed"));
        expect(last(requests).get("status")).toBe("RESERVED");
        expect(saved()).toEqual({ search: "typed", status: "RESERVED" });
      });

      it("a saved search equal to empty sends no search and persists empty with a filter change", async () => {
        const requests = setup();
        await ready();
        loadLate({ search: "", topology_type: "PHYSICAL" });
        pick("Status", "OFFLINE");
        await waitFor(() => expect(last(requests).get("status")).toBe("OFFLINE"));
        expect(last(requests).has("search")).toBe(false);
        expect(last(requests).get("topology_type")).toBe("PHYSICAL");
        expect(saved()).toEqual({ search: "", status: "OFFLINE", topology_type: "PHYSICAL" });
        expect(searchBox().value).toBe("");
      });
    });

    it("prunes expansion and clears selection when a filter changes the list", async () => {
      server.use(
        http.get("/api/inventory/templates", () =>
          HttpResponse.json({ items: [], total: 0, skip: 0, limit: 500 }),
        ),
        http.get("/api/inventory/devices", ({ request }) => {
          const onlyB = new URL(request.url).searchParams.get("status") === "OFFLINE";
          const items = onlyB
            ? [makeDevice({ id: idB, name: "dev-b" })]
            : [makeDevice({ id: idA, name: "dev-a" }), makeDevice({ id: idB, name: "dev-b" })];
          return HttpResponse.json({ items, total: items.length, skip: 0, limit: 50 });
        }),
        http.get("/api/inventory/devices/:id/ports", () => HttpResponse.json([])),
        http.get("/api/cabling/connections", () =>
          HttpResponse.json({ items: [], total: 0, skip: 0, limit: 500 }),
        ),
      );
      renderWithProviders(<InventoryPage />);
      await waitFor(() => expect(screen.getByText("dev-a")).toBeInTheDocument());
      const rowA = screen.getByText("dev-a").closest("tr") as HTMLElement;
      const rowB = screen.getByText("dev-b").closest("tr") as HTMLElement;
      fireEvent.click(within(rowA).getByLabelText("Expand ports"));
      fireEvent.click(within(rowB).getByLabelText("Expand ports"));
      fireEvent.click(within(rowB).getByRole("checkbox"));
      expect(screen.getByText("1 selected")).toBeInTheDocument();

      pick("Status", "OFFLINE");
      await waitFor(() => expect(screen.queryByText("dev-a")).not.toBeInTheDocument());
      expect(screen.queryByText("1 selected")).not.toBeInTheDocument();
      // dev-b stayed listed, so its panel is still open; dev-a's is gone.
      expect(screen.getAllByLabelText("Collapse ports")).toHaveLength(1);
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

    it("does not revert a Next-page click made right after mount (nightly run 33300868733)", async () => {
      // The search-debounce effect used to arm its 300ms setSkip(0) timer on
      // EVERY mount, search unchanged or not. A Next click landing inside
      // that window (setSkip(50)) was then silently clobbered when the
      // leftover mount-timer fired setSkip(0) a moment later:
      // tests/e2e/test_pagination.py::test_inventory_pagination_next_advances_page
      // timed out on a seeded nightly stack this way. Fake timers hold the
      // 300ms window open long enough to click Next before it elapses.
      const requests: { skip: string | null }[] = [];
      server.use(
        http.get("/api/inventory/devices", ({ request }) => {
          const url = new URL(request.url);
          const skip = url.searchParams.get("skip");
          requests.push({ skip });
          return HttpResponse.json({
            items: [makeDevice()],
            total: 150,
            skip: Number(skip ?? 0),
            limit: 50,
          });
        }),
      );

      vi.useFakeTimers();
      renderWithProviders(<InventoryPage />);
      await vi.waitFor(() => expect(screen.getByText("fw-edge-01")).toBeInTheDocument());

      // Click Next well inside the mount-effect's 300ms debounce window.
      await vi.advanceTimersByTimeAsync(50);
      fireEvent.click(screen.getByText("Next"));
      await vi.waitFor(() => expect(requests.some((r) => r.skip === "50")).toBe(true));

      // Now let the leftover mount-timer's deadline pass. It must not have
      // scheduled a setSkip(0) that fires here and reverts the page.
      await vi.advanceTimersByTimeAsync(300);
      expect(requests[requests.length - 1]?.skip).toBe("50");
      expect(screen.getByText("Page 2 of 3")).toBeInTheDocument();
      vi.useRealTimers();
    });
  });
});
