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
  }: {
    availableItems: { id: string }[];
    assignedItems: { id: string }[];
  }) => (
    <div data-testid="transfer-list">
      available:{availableItems.length} assigned:{assignedItems.length}
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
  // Admin user so the page does not redirect away.
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
    // d-1 is assigned, so only d-2 is available; the one assigned row stays.
    expect(screen.getByTestId("transfer-list")).toHaveTextContent(
      "available:1 assigned:1",
    );
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

  it("redirects non-admin users away from the page", async () => {
    useAuthStore.setState({
      user: { id: "2", username: "bob", email: "b@b.c", role: "user" },
    } as never);
    renderWithProviders(<DeviceGroupDetailPage />);

    await waitFor(() =>
      expect(navigateSpy).toHaveBeenCalledWith("/topology"),
    );
  });
});
