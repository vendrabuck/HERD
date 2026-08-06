import { render, screen, fireEvent, waitFor } from "@testing-library/react";
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

// Mock authStore (admin so the Copy/Delete actions render)
const mockUser = { id: "1", email: "admin@test.com", role: "admin", username: "admin" };
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
  { name: "General", fields: [{ key: "hostname", label: "Hostname", type: "string" as const }] },
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

const DYNAMIC_TEMPLATE: DeviceTemplate = {
  id: "t-dyn",
  name: "Ubuntu VM",
  template_type: "dynamic",
  driver_id: "drv-recipe",
  driver_name: "proxmox_vm",
  connection_type: "hypervisor",
  hypervisor_id: "hv-1",
  exclusive: true,
  icon: null,
  description: "hypervisor-backed vm",
  vendor: "unknown",
  model: "unknown",
  part_number: null,
  sections: SECTIONS,
  created_at: "2026-01-02T00:00:00Z",
  updated_at: "2026-01-02T00:00:00Z",
  poll_interval_seconds: null,
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
  mockCreateTemplate.mutateAsync.mockResolvedValue({});
  mockUsePaginatedTemplates.mockReturnValue({
    data: { items: [DEVICE_TEMPLATE, DYNAMIC_TEMPLATE], total: 2, skip: 0, limit: 50 },
    isLoading: false,
    isError: false,
  });
});

describe("TemplatesPage type filter", () => {
  it("offers a Dynamic option and filters by it (issue #473)", () => {
    renderPage();
    const select = screen.getByRole("combobox");
    const labels = Array.from(select.querySelectorAll("option")).map((o) => o.textContent);
    expect(labels).toEqual(["All", "Device", "Port", "Dynamic"]);

    fireEvent.change(select, { target: { value: "dynamic" } });
    expect(mockUsePaginatedTemplates).toHaveBeenLastCalledWith("dynamic", 0, 50);
  });

  it("renders dynamic rows returned by the filtered query", () => {
    mockUsePaginatedTemplates.mockReturnValue({
      data: { items: [DYNAMIC_TEMPLATE], total: 1, skip: 0, limit: 50 },
      isLoading: false,
      isError: false,
    });
    renderPage();
    expect(screen.getByText("Ubuntu VM")).toBeInTheDocument();
    expect(screen.getByText("dynamic")).toBeInTheDocument();
    expect(screen.queryByText("Edge Router")).not.toBeInTheDocument();
  });
});

describe("TemplatesPage copy", () => {
  it("copies a dynamic template with template_type, driver_id, and hypervisor_id (issue #473)", async () => {
    renderPage();
    // Row order follows the items array: [0] device, [1] dynamic.
    fireEvent.click(screen.getAllByTitle("Duplicate template")[1]);

    await waitFor(() => expect(mockCreateTemplate.mutateAsync).toHaveBeenCalledTimes(1));
    expect(mockCreateTemplate.mutateAsync).toHaveBeenCalledWith({
      name: "Copy of Ubuntu VM",
      template_type: "dynamic",
      driver_id: "drv-recipe",
      hypervisor_id: "hv-1",
      exclusive: true,
      icon: undefined,
      description: "hypervisor-backed vm",
      vendor: "unknown",
      model: "unknown",
      part_number: null,
      poll_interval_seconds: null,
      sections: SECTIONS,
    });
    await waitFor(() => expect(mockToastSuccess).toHaveBeenCalledWith("Template duplicated"));
  });

  it("copies a device template with its driver and identity fields", async () => {
    // The backend rejects a device template without driver_id, vendor, and
    // model, so the copy payload must carry them too.
    renderPage();
    fireEvent.click(screen.getAllByTitle("Duplicate template")[0]);

    await waitFor(() => expect(mockCreateTemplate.mutateAsync).toHaveBeenCalledTimes(1));
    expect(mockCreateTemplate.mutateAsync).toHaveBeenCalledWith({
      name: "Copy of Edge Router",
      template_type: "device",
      driver_id: "drv-ios",
      hypervisor_id: null,
      exclusive: true,
      icon: undefined,
      description: "router class",
      vendor: "Cisco",
      model: "ISR4451",
      part_number: "ISR4451-X/K9",
      poll_interval_seconds: 60,
      sections: SECTIONS,
    });
  });

  it("surfaces the backend detail on a copy failure", async () => {
    mockCreateTemplate.mutateAsync.mockRejectedValueOnce({
      response: { data: { detail: "Template with name 'Copy of Ubuntu VM' already exists" } },
    });
    renderPage();
    fireEvent.click(screen.getAllByTitle("Duplicate template")[1]);
    await waitFor(() =>
      expect(mockToastError).toHaveBeenCalledWith(
        "Template with name 'Copy of Ubuntu VM' already exists",
      ),
    );
  });
});
