import { http, HttpResponse } from "msw";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { describe, it, expect, vi, beforeAll, beforeEach } from "vitest";

beforeAll(() => {
  HTMLDialogElement.prototype.showModal = vi.fn(function (this: HTMLDialogElement) {
    this.open = true;
  });
  HTMLDialogElement.prototype.close = vi.fn(function (this: HTMLDialogElement) {
    this.open = false;
  });
});

// react-hot-toast is mocked so the success/error branches can be asserted
// without rendering a live toaster.
const toastSuccess = vi.fn();
const toastError = vi.fn();
vi.mock("react-hot-toast", () => ({
  default: { success: (m: string) => toastSuccess(m), error: (m: string) => toastError(m) },
}));

// useNavigate is stubbed so the Back button and post-delete navigation can be
// asserted without a real router transition.
const navigate = vi.fn();
vi.mock("react-router-dom", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-router-dom")>();
  return { ...actual, useNavigate: () => navigate };
});

// The real auth store is used (not a module mock) so client.ts's request
// interceptor can still call useAuthStore.getState(). Admin user is seeded in
// beforeEach via setState so the Edit and Delete controls render.
import { useAuthStore } from "@/stores/authStore";

// Heavy sub-components are exercised in their own suites. Stub them to keep
// this test focused on DevicePage's own logic (header, edit form, delete flow).
vi.mock("@/components/devices/DynamicFieldRenderer", () => ({
  DynamicFieldRenderer: () => <div data-testid="dynamic-field-renderer" />,
}));
vi.mock("@/components/devices/PortsSection", () => ({
  PortsSection: ({ deviceId }: { deviceId: string }) => (
    <div data-testid="ports-section">{deviceId}</div>
  ),
}));
vi.mock("@/components/device-config/DeviceConfigSection", () => ({
  DeviceConfigSection: ({ deviceId }: { deviceId: string }) => (
    <div data-testid="device-config-section">{deviceId}</div>
  ),
}));
vi.mock("@/components/inventory/DeviceInfoPanel", () => ({
  DeviceInfoPanel: ({ device }: { device: { name: string } }) => (
    <div data-testid="device-info-panel">{device.name}</div>
  ),
}));

import { server } from "../mocks/server";
import { DevicePage } from "@/pages/DevicePage";

const DEVICE_ID = "dev-1234";

const DEVICE = {
  id: DEVICE_ID,
  name: "core-fw-01",
  template_id: "tmpl-1",
  template_name: "PA-Series",
  template_icon: null,
  template_vendor: "Acme",
  template_model: "X1",
  template_part_number: "PN-1",
  topology_type: "PHYSICAL",
  status: "AVAILABLE",
  field_data: { mgmt_ip: "10.0.0.1", secret: "hunter2", enabled: true },
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
};

const TEMPLATE = {
  id: "tmpl-1",
  name: "PA-Series",
  sections: [
    {
      name: "Management",
      fields: [
        { key: "mgmt_ip", label: "Management IP", type: "text" },
        { key: "secret", label: "Secret", type: "password" },
        { key: "enabled", label: "Enabled", type: "boolean" },
      ],
    },
  ],
};

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[`/inventory/${DEVICE_ID}`]}>
        <Routes>
          <Route path="/inventory/:id" element={<DevicePage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  navigate.mockClear();
  toastSuccess.mockClear();
  toastError.mockClear();
  useAuthStore.setState({
    user: { id: "1", role: "admin", username: "admin", email: "a@b.c" },
  } as never);
  server.use(
    http.get(`/api/inventory/devices/${DEVICE_ID}`, () => HttpResponse.json(DEVICE)),
    http.get("/api/inventory/templates/tmpl-1", () => HttpResponse.json(TEMPLATE)),
  );
});

describe("DevicePage", () => {
  it("shows the loading state", () => {
    server.use(
      http.get(`/api/inventory/devices/${DEVICE_ID}`, async () => {
        await new Promise(() => {});
        return HttpResponse.json({});
      }),
    );
    renderPage();
    expect(screen.getByText(/Loading device/i)).toBeInTheDocument();
  });

  it("renders an error state when the device is not found", async () => {
    server.use(
      http.get(`/api/inventory/devices/${DEVICE_ID}`, () =>
        HttpResponse.json({ detail: "nope" }, { status: 404 }),
      ),
    );
    renderPage();
    await waitFor(() =>
      expect(screen.getByText("Device not found")).toBeInTheDocument(),
    );
  });

  it("renders device details with template fields and masks passwords", async () => {
    renderPage();
    await waitFor(() =>
      expect(screen.getByText("Device Details")).toBeInTheDocument(),
    );
    // Name appears in the info panel and the details header.
    expect(screen.getAllByText("core-fw-01").length).toBeGreaterThan(0);
    expect(screen.getByText("AVAILABLE")).toBeInTheDocument();
    expect(screen.getByText("PHYSICAL")).toBeInTheDocument();
    // Template-driven read-only fields render once the template query resolves.
    await screen.findByText("Management IP");
    expect(screen.getByText("10.0.0.1")).toBeInTheDocument();
    expect(screen.getByText("Yes")).toBeInTheDocument();
    // Password field is masked, never shown in clear text.
    expect(screen.getByText("********")).toBeInTheDocument();
    expect(screen.queryByText("hunter2")).not.toBeInTheDocument();
    // Sub-components received the device id.
    expect(screen.getByTestId("ports-section")).toHaveTextContent(DEVICE_ID);
    expect(screen.getByTestId("device-config-section")).toHaveTextContent(DEVICE_ID);
  });

  it("navigates back to inventory when Back is clicked", async () => {
    renderPage();
    await screen.findByText("Device Details");
    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    expect(navigate).toHaveBeenCalledWith("/inventory");
  });

  it("enters edit mode and shows editable name input prefilled from the device", async () => {
    renderPage();
    await screen.findByText("Device Details");
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    expect(screen.getByText("Edit Device")).toBeInTheDocument();
    const nameInput = screen.getByLabelText("Name") as HTMLInputElement;
    expect(nameInput.value).toBe("core-fw-01");
  });

  it("blocks save with an error toast when the name is cleared", async () => {
    renderPage();
    await screen.findByText("Device Details");
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    const nameInput = screen.getByLabelText("Name");
    fireEvent.change(nameInput, { target: { value: "   " } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(toastError).toHaveBeenCalledWith("Name is required"));
  });

  it("saves an edited name and returns to the detail view", async () => {
    let putBody: { name?: string } | undefined;
    server.use(
      http.put(`/api/inventory/devices/${DEVICE_ID}`, async ({ request }) => {
        putBody = (await request.json()) as { name?: string };
        return HttpResponse.json({ ...DEVICE, name: "renamed-fw" });
      }),
    );
    renderPage();
    await screen.findByText("Device Details");
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.change(screen.getByLabelText("Name"), {
      target: { value: "renamed-fw" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith("Device updated"));
    expect(putBody?.name).toBe("renamed-fw");
    await waitFor(() =>
      expect(screen.getByText("Device Details")).toBeInTheDocument(),
    );
  });

  it("deletes the device and navigates to inventory on confirm", async () => {
    server.use(
      http.delete(`/api/inventory/devices/${DEVICE_ID}`, () =>
        new HttpResponse(null, { status: 204 }),
      ),
    );
    renderPage();
    await screen.findByText("Device Details");
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    // ConfirmDialog renders a destructive Delete button.
    const confirmButtons = screen.getAllByRole("button", { name: "Delete" });
    // The dialog's confirm button is the second Delete control.
    fireEvent.click(confirmButtons[confirmButtons.length - 1]);
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith("Device deleted"));
    expect(navigate).toHaveBeenCalledWith("/inventory");
  });

  it("surfaces the server detail message when an update fails", async () => {
    server.use(
      http.put(`/api/inventory/devices/${DEVICE_ID}`, () =>
        HttpResponse.json({ detail: "name already taken" }, { status: 409 }),
      ),
    );
    renderPage();
    await screen.findByText("Device Details");
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.change(screen.getByLabelText("Name"), {
      target: { value: "dup-name" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith("name already taken"),
    );
  });
});
