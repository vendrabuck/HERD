import { http, HttpResponse } from "msw";
import { render, screen, waitFor, fireEvent, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type { ReactNode } from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";

// setup.ts already polyfills HTMLDialogElement.showModal/close to toggle the
// `open` attribute, so dialogs become queryable by the "dialog" role. Do not
// override it with no-op spies here, or the dialog never opens.

// Toasts are fire-and-forget side effects; stub so save/delete paths do not
// blow up and so we can assert on the messages they emit. Declared via
// vi.hoisted so the mock factory (hoisted above this file) can reference it.
const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }));
vi.mock("react-hot-toast", () => ({ default: toast }));

// useParams drives whether the page is in "create" (no id) or "edit" (id) mode,
// and useNavigate is a side effect we want to observe. We keep the real
// MemoryRouter so the QueryClient + Router providers behave normally.
let routeId: string | undefined;
const navigateSpy = vi.fn();
vi.mock("react-router-dom", async () => {
  const actual =
    await vi.importActual<typeof import("react-router-dom")>("react-router-dom");
  return {
    ...actual,
    useParams: () => ({ id: routeId }),
    useNavigate: () => navigateSpy,
  };
});

// The transfer list and modal are exercised in their own component tests. Stub
// them so this page test can focus on data wiring, headings, and the table/
// empty states rather than the drag-between-lists interaction.
vi.mock("@/components/ui/TransferList", () => ({
  TransferList: ({
    availableItems,
    assignedItems,
    onAssign,
    onUnassign,
  }: {
    availableItems: { id: string }[];
    assignedItems: { id: string }[];
    onAssign: (ids: string[]) => void;
    onUnassign: (ids: string[]) => void;
  }) => (
    <div data-testid="transfer-list">
      available:{availableItems.length} assigned:{assignedItems.length}
      <span data-testid="available-ids">{availableItems.map((i) => i.id).join(",")}</span>
      <span data-testid="assigned-ids">{assignedItems.map((i) => i.id).join(",")}</span>
      <button onClick={() => onAssign(["target-added"])}>call-assign</button>
      <button onClick={() => onUnassign(["target-removed"])}>call-unassign</button>
    </div>
  ),
}));

vi.mock("@/components/ui/Modal", () => ({
  Modal: ({
    open,
    title,
    children,
  }: {
    open: boolean;
    title: string;
    children: ReactNode;
  }) =>
    open ? (
      <div data-testid="modal" data-title={title}>
        {children}
      </div>
    ) : null,
}));

import { server } from "../mocks/server";
import { DeviceGroupDetailPage } from "@/pages/admin/DeviceGroupDetailPage";
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

const GROUP_ID = "11111111-2222-3333-4444-555555555555";

function makeGroupDetail(overrides: Record<string, unknown> = {}) {
  return {
    id: GROUP_ID,
    name: "Edge Firewalls",
    description: "Perimeter devices",
    created_by: null,
    created_at: "2026-01-01T00:00:00Z",
    device_count: 1,
    user_group_count: 1,
    devices: [
      {
        device_id: "d-1",
        device_name: "fw-edge-01",
        template_name: "FW-3600",
        added_at: "2026-01-02T00:00:00Z",
      },
    ],
    user_groups: [
      {
        user_group_id: "ug-1",
        user_group_name: "netops",
        assigned_by: null,
        assigned_at: "2026-01-03T00:00:00Z",
      },
    ],
    ...overrides,
  };
}

beforeEach(() => {
  routeId = GROUP_ID;
  navigateSpy.mockClear();
  toast.success.mockClear();
  toast.error.mockClear();
  // The page assumes an admin user; the redirect for non-admins now lives
  // in the AdminGuard route group in routes.tsx, tested in AdminGuard.test.tsx.
  useAuthStore.setState({
    user: { id: "1", username: "admin", email: "a@b.c", role: "admin" },
  } as never);
  // Supporting lists used to compute available transfer items.
  server.use(
    http.get("/api/inventory/devices", () =>
      HttpResponse.json({
        items: [
          {
            id: "d-1",
            name: "fw-edge-01",
            template_name: "FW-3600",
          },
          {
            id: "d-2",
            name: "fw-edge-02",
            template_name: "FW-3600",
          },
        ],
        total: 2,
        skip: 0,
        limit: 500,
      }),
    ),
    http.get("/api/auth/groups", () =>
      HttpResponse.json({
        items: [
          { id: "ug-1", name: "netops", description: null },
          { id: "ug-2", name: "labusers", description: null },
        ],
        total: 2,
        skip: 0,
        limit: 500,
      }),
    ),
  );
});

describe("DeviceGroupDetailPage", () => {
  it("renders the create form when there is no route id", () => {
    routeId = undefined;
    renderWithProviders(<DeviceGroupDetailPage />);

    expect(screen.getByText("Create Device Group")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create" })).toBeInTheDocument();
    // No delete button and no device/permission sections before a group exists.
    expect(
      screen.queryByRole("button", { name: "Delete Device Group" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByText(/^Devices \(/)).not.toBeInTheDocument();
  });

  it("shows the loading state while the group is fetched", () => {
    server.use(
      http.get(`/api/inventory/device-groups/${GROUP_ID}`, async () => {
        await new Promise(() => {});
        return HttpResponse.json({});
      }),
    );
    renderWithProviders(<DeviceGroupDetailPage />);
    expect(screen.getByText(/Loading device group/i)).toBeInTheDocument();
  });

  it("renders the populated group with its device and permission rows", async () => {
    server.use(
      http.get(`/api/inventory/device-groups/${GROUP_ID}`, () =>
        HttpResponse.json(makeGroupDetail()),
      ),
    );
    renderWithProviders(<DeviceGroupDetailPage />);

    // Name field is hydrated from the fetched group.
    await waitFor(() =>
      expect(screen.getByLabelText("Name")).toHaveValue("Edge Firewalls"),
    );
    expect(screen.getByText("Edit Device Group")).toBeInTheDocument();
    expect(screen.getByText("Devices (1)")).toBeInTheDocument();
    expect(screen.getByText("fw-edge-01")).toBeInTheDocument();
    expect(screen.getByText("User Group Permissions (1)")).toBeInTheDocument();
    expect(screen.getByText("netops")).toBeInTheDocument();
  });

  it("renders empty section copy when the group has no devices or permissions", async () => {
    server.use(
      http.get(`/api/inventory/device-groups/${GROUP_ID}`, () =>
        HttpResponse.json(
          makeGroupDetail({ devices: [], user_groups: [] }),
        ),
      ),
    );
    renderWithProviders(<DeviceGroupDetailPage />);

    await waitFor(() =>
      expect(screen.getByText("No devices assigned")).toBeInTheDocument(),
    );
    expect(screen.getByText("No user groups assigned")).toBeInTheDocument();
    expect(screen.getByText("Devices (0)")).toBeInTheDocument();
  });

  it("opens the device transfer modal and excludes already-assigned devices", async () => {
    server.use(
      http.get(`/api/inventory/device-groups/${GROUP_ID}`, () =>
        HttpResponse.json(makeGroupDetail()),
      ),
    );
    renderWithProviders(<DeviceGroupDetailPage />);

    await screen.findByText("fw-edge-01");
    expect(screen.queryByTestId("modal")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Add or Remove Devices" }));

    const modal = await screen.findByTestId("modal");
    expect(modal).toHaveAttribute("data-title", "Add or Remove Devices");
    // d-1 is assigned, so only d-2 is available; the one assigned row stays
    // as d-1, not merely a count match.
    expect(screen.getByTestId("transfer-list")).toHaveTextContent(
      "available:1 assigned:1",
    );
    expect(screen.getByTestId("available-ids")).toHaveTextContent("d-2");
    expect(screen.getByTestId("assigned-ids")).toHaveTextContent("d-1");
  });

  it("validates that a name is required before saving", async () => {
    server.use(
      http.get(`/api/inventory/device-groups/${GROUP_ID}`, () =>
        HttpResponse.json(makeGroupDetail({ name: "" })),
      ),
    );
    renderWithProviders(<DeviceGroupDetailPage />);

    await waitFor(() =>
      expect(screen.getByText("Edit Device Group")).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        "Device group name is required",
      ),
    );
  });

  // Non-admin redirect coverage moved to AdminGuard.test.tsx: the guard now
  // lives in the AdminGuard route group in routes.tsx (issue #527), and this
  // page no longer performs its own redirect check.

  it("creates a device group and navigates to its detail route", async () => {
    routeId = undefined;
    let captured: unknown = null;
    server.use(
      http.post("/api/inventory/device-groups", async ({ request }) => {
        captured = await request.json();
        return HttpResponse.json({ id: "new-dg", name: "New Lab", description: null });
      }),
    );
    renderWithProviders(<DeviceGroupDetailPage />);

    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "New Lab" } });
    fireEvent.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() => expect(toast.success).toHaveBeenCalledWith("Device group created"));
    expect(captured).toEqual({ name: "New Lab", description: null });
    expect(navigateSpy).toHaveBeenCalledWith("/admin/device-groups/new-dg", { replace: true });
  });

  it("saves an edit to an existing group without navigating", async () => {
    let captured: unknown = null;
    server.use(
      http.get(`/api/inventory/device-groups/${GROUP_ID}`, () =>
        HttpResponse.json(makeGroupDetail()),
      ),
      http.put(`/api/inventory/device-groups/${GROUP_ID}`, async ({ request }) => {
        captured = await request.json();
        return HttpResponse.json(makeGroupDetail({ name: "Renamed Firewalls" }));
      }),
    );
    renderWithProviders(<DeviceGroupDetailPage />);
    await waitFor(() => expect(screen.getByLabelText("Name")).toHaveValue("Edge Firewalls"));

    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Renamed Firewalls" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(toast.success).toHaveBeenCalledWith("Device group updated"));
    expect(captured).toEqual({ name: "Renamed Firewalls", description: "Perimeter devices" });
    expect(navigateSpy).not.toHaveBeenCalled();
  });

  it("surfaces the server detail message when save fails", async () => {
    server.use(
      http.get(`/api/inventory/device-groups/${GROUP_ID}`, () =>
        HttpResponse.json(makeGroupDetail()),
      ),
      http.put(`/api/inventory/device-groups/${GROUP_ID}`, () =>
        HttpResponse.json({ detail: "name already taken" }, { status: 409 }),
      ),
    );
    renderWithProviders(<DeviceGroupDetailPage />);
    await waitFor(() => expect(screen.getByLabelText("Name")).toHaveValue("Edge Firewalls"));

    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(toast.error).toHaveBeenCalledWith("name already taken"));
  });

  it("falls back to a generic message when save fails with no detail", async () => {
    server.use(
      http.get(`/api/inventory/device-groups/${GROUP_ID}`, () =>
        HttpResponse.json(makeGroupDetail()),
      ),
      http.put(`/api/inventory/device-groups/${GROUP_ID}`, () =>
        new HttpResponse(null, { status: 500 }),
      ),
    );
    renderWithProviders(<DeviceGroupDetailPage />);
    await waitFor(() => expect(screen.getByLabelText("Name")).toHaveValue("Edge Firewalls"));

    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith("Failed to save device group"),
    );
  });

  it("deletes the group through the confirm dialog and navigates to the list", async () => {
    let deleteCalled = false;
    server.use(
      http.get(`/api/inventory/device-groups/${GROUP_ID}`, () =>
        HttpResponse.json(makeGroupDetail()),
      ),
      http.delete(`/api/inventory/device-groups/${GROUP_ID}`, () => {
        deleteCalled = true;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    renderWithProviders(<DeviceGroupDetailPage />);
    await screen.findByText("fw-edge-01");

    fireEvent.click(screen.getByRole("button", { name: "Delete Device Group" }));
    const dialog = screen.getByRole("dialog", { name: "Delete Device Group" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete" }));

    await waitFor(() => expect(deleteCalled).toBe(true));
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith("Device group deleted"));
    expect(navigateSpy).toHaveBeenCalledWith("/admin/device-groups");
  });

  it("cancelling the delete confirm dialog does not call delete or navigate", async () => {
    let deleteCalled = false;
    server.use(
      http.get(`/api/inventory/device-groups/${GROUP_ID}`, () =>
        HttpResponse.json(makeGroupDetail()),
      ),
      http.delete(`/api/inventory/device-groups/${GROUP_ID}`, () => {
        deleteCalled = true;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    renderWithProviders(<DeviceGroupDetailPage />);
    await screen.findByText("fw-edge-01");

    fireEvent.click(screen.getByRole("button", { name: "Delete Device Group" }));
    const dialog = screen.getByRole("dialog", { name: "Delete Device Group" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));

    expect(deleteCalled).toBe(false);
    expect(navigateSpy).not.toHaveBeenCalled();
  });

  it("surfaces the server detail message when delete fails", async () => {
    server.use(
      http.get(`/api/inventory/device-groups/${GROUP_ID}`, () =>
        HttpResponse.json(makeGroupDetail()),
      ),
      http.delete(`/api/inventory/device-groups/${GROUP_ID}`, () =>
        HttpResponse.json({ detail: "group still referenced" }, { status: 409 }),
      ),
    );
    renderWithProviders(<DeviceGroupDetailPage />);
    await screen.findByText("fw-edge-01");

    fireEvent.click(screen.getByRole("button", { name: "Delete Device Group" }));
    const dialog = screen.getByRole("dialog", { name: "Delete Device Group" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete" }));

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith("group still referenced"),
    );
    expect(navigateSpy).not.toHaveBeenCalled();
  });

  it("opens the permission transfer modal and excludes already-assigned user groups", async () => {
    server.use(
      http.get(`/api/inventory/device-groups/${GROUP_ID}`, () =>
        HttpResponse.json(makeGroupDetail()),
      ),
    );
    renderWithProviders(<DeviceGroupDetailPage />);
    await screen.findByText("netops");

    fireEvent.click(screen.getByRole("button", { name: "Add or Remove Permissions" }));

    const modal = await screen.findByTestId("modal");
    expect(modal).toHaveAttribute("data-title", "Add or Remove Permissions");
    // ug-1 is assigned, so only ug-2 (labusers) is available; the assigned
    // row stays as ug-1, not merely a count match.
    expect(screen.getByTestId("transfer-list")).toHaveTextContent(
      "available:1 assigned:1",
    );
    expect(screen.getByTestId("available-ids")).toHaveTextContent("ug-2");
    expect(screen.getByTestId("assigned-ids")).toHaveTextContent("ug-1");
  });
});

// Hooks exercised directly to drive the assign/unassign callbacks the mocked
// TransferList does not expose buttons for above; these tests replace the
// TransferList mock with one that exposes onAssign/onUnassign directly.
describe("DeviceGroupDetailPage bulk device and permission mutations", () => {
  it("adds devices through the transfer callback and reports added/skipped", async () => {
    server.use(
      http.get(`/api/inventory/device-groups/${GROUP_ID}`, () =>
        HttpResponse.json(makeGroupDetail()),
      ),
      http.post(`/api/inventory/device-groups/${GROUP_ID}/devices/bulk`, () =>
        HttpResponse.json({ added: 1, skipped: 1 }),
      ),
    );
    renderWithProviders(<DeviceGroupDetailPage />);
    await screen.findByText("fw-edge-01");
    fireEvent.click(screen.getByRole("button", { name: "Add or Remove Devices" }));
    await screen.findByTestId("modal");

    fireEvent.click(screen.getByRole("button", { name: "call-assign" }));

    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith("1 device(s) added, 1 skipped"),
    );
  });

  it("surfaces the server detail message when adding devices fails", async () => {
    server.use(
      http.get(`/api/inventory/device-groups/${GROUP_ID}`, () =>
        HttpResponse.json(makeGroupDetail()),
      ),
      http.post(`/api/inventory/device-groups/${GROUP_ID}/devices/bulk`, () =>
        HttpResponse.json({ detail: "device already claimed" }, { status: 409 }),
      ),
    );
    renderWithProviders(<DeviceGroupDetailPage />);
    await screen.findByText("fw-edge-01");
    fireEvent.click(screen.getByRole("button", { name: "Add or Remove Devices" }));
    await screen.findByTestId("modal");

    fireEvent.click(screen.getByRole("button", { name: "call-assign" }));

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith("device already claimed"),
    );
  });

  it("falls back to a generic message when adding devices fails with no detail", async () => {
    server.use(
      http.get(`/api/inventory/device-groups/${GROUP_ID}`, () =>
        HttpResponse.json(makeGroupDetail()),
      ),
      http.post(`/api/inventory/device-groups/${GROUP_ID}/devices/bulk`, () =>
        new HttpResponse(null, { status: 500 }),
      ),
    );
    renderWithProviders(<DeviceGroupDetailPage />);
    await screen.findByText("fw-edge-01");
    fireEvent.click(screen.getByRole("button", { name: "Add or Remove Devices" }));
    await screen.findByTestId("modal");

    fireEvent.click(screen.getByRole("button", { name: "call-assign" }));

    await waitFor(() => expect(toast.error).toHaveBeenCalledWith("Failed to add devices"));
  });

  it("removes devices through the transfer callback and reports the removed count", async () => {
    server.use(
      http.get(`/api/inventory/device-groups/${GROUP_ID}`, () =>
        HttpResponse.json(makeGroupDetail()),
      ),
      http.post(`/api/inventory/device-groups/${GROUP_ID}/devices/bulk-remove`, () =>
        HttpResponse.json({ removed: 1, not_found: 0 }),
      ),
    );
    renderWithProviders(<DeviceGroupDetailPage />);
    await screen.findByText("fw-edge-01");
    fireEvent.click(screen.getByRole("button", { name: "Add or Remove Devices" }));
    await screen.findByTestId("modal");

    fireEvent.click(screen.getByRole("button", { name: "call-unassign" }));

    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith("1 device(s) removed"),
    );
  });

  it("surfaces the server detail message when removing devices fails", async () => {
    server.use(
      http.get(`/api/inventory/device-groups/${GROUP_ID}`, () =>
        HttpResponse.json(makeGroupDetail()),
      ),
      http.post(`/api/inventory/device-groups/${GROUP_ID}/devices/bulk-remove`, () =>
        HttpResponse.json({ detail: "device not in group" }, { status: 404 }),
      ),
    );
    renderWithProviders(<DeviceGroupDetailPage />);
    await screen.findByText("fw-edge-01");
    fireEvent.click(screen.getByRole("button", { name: "Add or Remove Devices" }));
    await screen.findByTestId("modal");

    fireEvent.click(screen.getByRole("button", { name: "call-unassign" }));

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith("device not in group"),
    );
  });

  it("falls back to a generic message when removing devices fails with no detail", async () => {
    server.use(
      http.get(`/api/inventory/device-groups/${GROUP_ID}`, () =>
        HttpResponse.json(makeGroupDetail()),
      ),
      http.post(`/api/inventory/device-groups/${GROUP_ID}/devices/bulk-remove`, () =>
        new HttpResponse(null, { status: 500 }),
      ),
    );
    renderWithProviders(<DeviceGroupDetailPage />);
    await screen.findByText("fw-edge-01");
    fireEvent.click(screen.getByRole("button", { name: "Add or Remove Devices" }));
    await screen.findByTestId("modal");

    fireEvent.click(screen.getByRole("button", { name: "call-unassign" }));

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith("Failed to remove devices"),
    );
  });

  it("adds user group permissions through the transfer callback", async () => {
    server.use(
      http.get(`/api/inventory/device-groups/${GROUP_ID}`, () =>
        HttpResponse.json(makeGroupDetail()),
      ),
      http.post(`/api/inventory/device-groups/${GROUP_ID}/permissions/bulk`, () =>
        HttpResponse.json({ added: 1, skipped: 0 }),
      ),
    );
    renderWithProviders(<DeviceGroupDetailPage />);
    await screen.findByText("netops");
    fireEvent.click(screen.getByRole("button", { name: "Add or Remove Permissions" }));
    await screen.findByTestId("modal");

    fireEvent.click(screen.getByRole("button", { name: "call-assign" }));

    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith("1 user group(s) assigned"),
    );
  });

  it("surfaces the server detail message when adding permissions fails", async () => {
    server.use(
      http.get(`/api/inventory/device-groups/${GROUP_ID}`, () =>
        HttpResponse.json(makeGroupDetail()),
      ),
      http.post(`/api/inventory/device-groups/${GROUP_ID}/permissions/bulk`, () =>
        HttpResponse.json({ detail: "user group already assigned" }, { status: 409 }),
      ),
    );
    renderWithProviders(<DeviceGroupDetailPage />);
    await screen.findByText("netops");
    fireEvent.click(screen.getByRole("button", { name: "Add or Remove Permissions" }));
    await screen.findByTestId("modal");

    fireEvent.click(screen.getByRole("button", { name: "call-assign" }));

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith("user group already assigned"),
    );
  });

  it("falls back to a generic message when adding permissions fails with no detail", async () => {
    server.use(
      http.get(`/api/inventory/device-groups/${GROUP_ID}`, () =>
        HttpResponse.json(makeGroupDetail()),
      ),
      http.post(`/api/inventory/device-groups/${GROUP_ID}/permissions/bulk`, () =>
        new HttpResponse(null, { status: 500 }),
      ),
    );
    renderWithProviders(<DeviceGroupDetailPage />);
    await screen.findByText("netops");
    fireEvent.click(screen.getByRole("button", { name: "Add or Remove Permissions" }));
    await screen.findByTestId("modal");

    fireEvent.click(screen.getByRole("button", { name: "call-assign" }));

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith("Failed to assign user groups"),
    );
  });

  it("removes user group permissions through the transfer callback", async () => {
    server.use(
      http.get(`/api/inventory/device-groups/${GROUP_ID}`, () =>
        HttpResponse.json(makeGroupDetail()),
      ),
      http.post(`/api/inventory/device-groups/${GROUP_ID}/permissions/bulk-remove`, () =>
        HttpResponse.json({ removed: 1, not_found: 0 }),
      ),
    );
    renderWithProviders(<DeviceGroupDetailPage />);
    await screen.findByText("netops");
    fireEvent.click(screen.getByRole("button", { name: "Add or Remove Permissions" }));
    await screen.findByTestId("modal");

    fireEvent.click(screen.getByRole("button", { name: "call-unassign" }));

    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith("1 user group(s) removed"),
    );
  });

  it("surfaces the server detail message when removing permissions fails", async () => {
    server.use(
      http.get(`/api/inventory/device-groups/${GROUP_ID}`, () =>
        HttpResponse.json(makeGroupDetail()),
      ),
      http.post(`/api/inventory/device-groups/${GROUP_ID}/permissions/bulk-remove`, () =>
        HttpResponse.json({ detail: "user group not assigned" }, { status: 404 }),
      ),
    );
    renderWithProviders(<DeviceGroupDetailPage />);
    await screen.findByText("netops");
    fireEvent.click(screen.getByRole("button", { name: "Add or Remove Permissions" }));
    await screen.findByTestId("modal");

    fireEvent.click(screen.getByRole("button", { name: "call-unassign" }));

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith("user group not assigned"),
    );
  });

  it("falls back to a generic message when removing permissions fails with no detail", async () => {
    server.use(
      http.get(`/api/inventory/device-groups/${GROUP_ID}`, () =>
        HttpResponse.json(makeGroupDetail()),
      ),
      http.post(`/api/inventory/device-groups/${GROUP_ID}/permissions/bulk-remove`, () =>
        new HttpResponse(null, { status: 500 }),
      ),
    );
    renderWithProviders(<DeviceGroupDetailPage />);
    await screen.findByText("netops");
    fireEvent.click(screen.getByRole("button", { name: "Add or Remove Permissions" }));
    await screen.findByTestId("modal");

    fireEvent.click(screen.getByRole("button", { name: "call-unassign" }));

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith("Failed to remove user groups"),
    );
  });
});
