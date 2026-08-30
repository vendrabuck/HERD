import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, vi, beforeEach } from "vitest";

// Mock react-router-dom
const mockNavigate = vi.fn();
vi.mock("react-router-dom", () => ({
  useNavigate: () => mockNavigate,
}));

// Mock react-hot-toast
const mockToastError = vi.fn();
const mockToastSuccess = vi.fn();
vi.mock("react-hot-toast", () => ({
  default: {
    error: (...args: unknown[]) => mockToastError(...args),
    success: (...args: unknown[]) => mockToastSuccess(...args),
  },
}));

// Mock the bulk import/export API so BulkImportExport renders inert.
vi.mock("@/api/bulk", () => ({
  exportTemplates: vi.fn(),
  importTemplates: vi.fn(),
}));

// Mock authStore (default non-admin for this file; overridden per-test)
let mockUser: { id: string; role: string } | null = { id: "user-1", role: "user" };
vi.mock("@/stores/authStore", () => ({
  useAuthStore: (selector: (s: { user: typeof mockUser }) => unknown) =>
    selector({ user: mockUser }),
}));

// Mock template hooks
const mockUsePaginatedTemplates = vi.fn();
const mockCreateTemplate = { mutateAsync: vi.fn(), isPending: false };
const mockDeleteTemplate = { mutateAsync: vi.fn(), isPending: false };

vi.mock("@/api/templates", () => ({
  usePaginatedTemplates: (...args: unknown[]) => mockUsePaginatedTemplates(...args),
  useCreateTemplate: () => mockCreateTemplate,
  useDeleteTemplate: () => mockDeleteTemplate,
}));

import { TemplatesPage } from "@/pages/TemplatesPage";
import type { DeviceTemplate } from "@/types/template.types";

const SECTIONS = [
  {
    name: "General",
    fields: [
      { key: "hostname", label: "Hostname", type: "string" as const },
      { key: "ip", label: "IP", type: "string" as const },
    ],
  },
  { name: "Advanced", fields: [{ key: "mtu", label: "MTU", type: "number" as const }] },
];

const DEVICE_TEMPLATE: DeviceTemplate = {
  id: "t-device",
  name: "Edge Router",
  template_type: "device",
  driver_id: "drv-ios",
  driver_name: "cisco_ios",
  connection_type: "management",
  hypervisor_id: null,
  exclusive: true,
  icon: null,
  description: "router class",
  vendor: "Cisco",
  model: "ISR4451",
  part_number: "ISR4451-X/K9",
  sections: SECTIONS,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
  poll_interval_seconds: 60,
};

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <TemplatesPage />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  mockUser = { id: "user-1", role: "user" };
  mockCreateTemplate.mutateAsync.mockResolvedValue({});
  mockDeleteTemplate.mutateAsync.mockResolvedValue(undefined);
  mockUsePaginatedTemplates.mockReturnValue({
    data: { items: [DEVICE_TEMPLATE], total: 1, skip: 0, limit: 50 },
    isLoading: false,
    isError: false,
  });
});

describe("TemplatesPage loading/error/empty states", () => {
  it("shows the loading status text while fetching", () => {
    mockUsePaginatedTemplates.mockReturnValue({ data: undefined, isLoading: true, isError: false });
    renderPage();
    expect(screen.getByRole("status")).toHaveTextContent("Loading templates...");
  });

  it("shows the exact error text on a failed fetch", () => {
    mockUsePaginatedTemplates.mockReturnValue({ data: undefined, isLoading: false, isError: true });
    renderPage();
    expect(screen.getByText("Failed to load templates")).toBeInTheDocument();
  });

  it("shows the exact empty-state text when there are zero templates", () => {
    mockUsePaginatedTemplates.mockReturnValue({
      data: { items: [], total: 0, skip: 0, limit: 50 },
      isLoading: false,
      isError: false,
    });
    renderPage();
    expect(screen.getByText("No templates found")).toBeInTheDocument();
  });
});

describe("TemplatesPage row rendering and non-admin visibility", () => {
  it("renders name, type, description, section count, and field count", () => {
    renderPage();
    expect(screen.getByText("Edge Router")).toBeInTheDocument();
    expect(screen.getByText("device")).toBeInTheDocument();
    expect(screen.getByText("router class")).toBeInTheDocument();
    // 2 sections; 3 total fields (2 + 1).
    expect(screen.getByText("2")).toBeInTheDocument();
    expect(screen.getByText("3")).toBeInTheDocument();
  });

  it("shows a dash for a null description", () => {
    mockUsePaginatedTemplates.mockReturnValue({
      data: { items: [{ ...DEVICE_TEMPLATE, description: null }], total: 1, skip: 0, limit: 50 },
      isLoading: false,
      isError: false,
    });
    renderPage();
    expect(screen.getByText("-")).toBeInTheDocument();
  });

  it("a non-admin sees no Create Template button and no Actions column", () => {
    renderPage();
    expect(screen.queryByRole("button", { name: "Create Template" })).not.toBeInTheDocument();
    expect(screen.queryByText("Actions")).not.toBeInTheDocument();
    expect(screen.queryByTitle("Duplicate template")).not.toBeInTheDocument();
  });

  it("an admin sees the Create Template button and Actions column", () => {
    mockUser = { id: "user-1", role: "admin" };
    renderPage();
    expect(screen.getByRole("button", { name: "Create Template" })).toBeInTheDocument();
    expect(screen.getByText("Actions")).toBeInTheDocument();
  });

  it("clicking a row navigates to its editor page", () => {
    renderPage();
    fireEvent.click(screen.getByText("Edge Router"));
    expect(mockNavigate).toHaveBeenCalledWith("/templates/t-device");
  });

  it("Create Template navigates to the new-template route", () => {
    mockUser = { id: "user-1", role: "admin" };
    renderPage();
    fireEvent.click(screen.getByRole("button", { name: "Create Template" }));
    expect(mockNavigate).toHaveBeenCalledWith("/templates/new");
  });
});

describe("TemplatesPage type filter resets pagination", () => {
  it("changing the type filter resets skip back to 0", () => {
    renderPage();
    const select = screen.getByRole("combobox");
    fireEvent.change(select, { target: { value: "port" } });
    expect(mockUsePaginatedTemplates).toHaveBeenLastCalledWith("port", 0, 50);
  });

  it("selecting All passes undefined rather than an empty string", () => {
    renderPage();
    const select = screen.getByRole("combobox");
    fireEvent.change(select, { target: { value: "device" } });
    fireEvent.change(select, { target: { value: "" } });
    expect(mockUsePaginatedTemplates).toHaveBeenLastCalledWith(undefined, 0, 50);
  });
});

describe("TemplatesPage delete flow", () => {
  beforeEach(() => {
    mockUser = { id: "user-1", role: "admin" };
  });

  it("clicking Delete opens a confirm dialog with the template name in the description", () => {
    renderPage();
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    expect(screen.getByText("Delete Template")).toBeInTheDocument();
    expect(screen.getByText(/Delete "Edge Router"\?/)).toBeInTheDocument();
  });

  it("confirming delete calls the mutation with the row id and toasts success", async () => {
    renderPage();
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    const dialog = screen.getByText("Delete Template").closest("dialog")!;
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete" }));

    await waitFor(() => expect(mockDeleteTemplate.mutateAsync).toHaveBeenCalledWith("t-device"));
    await waitFor(() => expect(mockToastSuccess).toHaveBeenCalledWith("Template deleted"));
  });

  it("cancelling the confirm dialog does not call delete", () => {
    renderPage();
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    const dialog = screen.getByText("Delete Template").closest("dialog")!;
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(mockDeleteTemplate.mutateAsync).not.toHaveBeenCalled();
  });

  it("surfaces the backend detail toast on a failed delete", async () => {
    mockDeleteTemplate.mutateAsync.mockRejectedValueOnce({
      response: { data: { detail: "Template has existing devices" } },
    });
    renderPage();
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    const dialog = screen.getByText("Delete Template").closest("dialog")!;
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete" }));

    await waitFor(() =>
      expect(mockToastError).toHaveBeenCalledWith("Template has existing devices"),
    );
    expect(mockToastSuccess).not.toHaveBeenCalled();
  });

  it("falls back to a generic delete-failure message with no response detail", async () => {
    mockDeleteTemplate.mutateAsync.mockRejectedValueOnce(new Error("network down"));
    renderPage();
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    const dialog = screen.getByText("Delete Template").closest("dialog")!;
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete" }));

    await waitFor(() =>
      expect(mockToastError).toHaveBeenCalledWith("Failed to delete template"),
    );
  });

  it("clicking a row's Delete button does not also navigate to the editor", () => {
    renderPage();
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    expect(mockNavigate).not.toHaveBeenCalled();
  });
});
