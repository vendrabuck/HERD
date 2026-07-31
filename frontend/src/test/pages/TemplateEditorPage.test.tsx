import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";

// Mock react-router-dom
const mockNavigate = vi.fn();
vi.mock("react-router-dom", () => ({
  useNavigate: () => mockNavigate,
  useParams: () => ({ id: "new" }),
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

// Mock authStore
const mockUser = { id: "1", email: "admin@test.com", role: "admin", username: "admin" };
vi.mock("@/stores/authStore", () => ({
  useAuthStore: (selector: (s: { user: typeof mockUser }) => unknown) =>
    selector({ user: mockUser }),
}));

// Mock template hooks
const mockUseTemplate = vi.fn();
const mockCreateTemplate = { mutateAsync: vi.fn(), isPending: false };
const mockUpdateTemplate = { mutateAsync: vi.fn(), isPending: false };
vi.mock("@/api/templates", () => ({
  useTemplate: (...args: unknown[]) => mockUseTemplate(...args),
  useCreateTemplate: () => mockCreateTemplate,
  useUpdateTemplate: () => mockUpdateTemplate,
}));

// Mock driver hooks
const mockUseDrivers = vi.fn();
vi.mock("@/api/drivers", () => ({
  useDrivers: () => mockUseDrivers(),
}));

// Mock hypervisor hooks
const mockUseHypervisors = vi.fn();
vi.mock("@/api/hypervisors", () => ({
  useHypervisors: () => mockUseHypervisors(),
}));

// Mock AI hooks (not under test here, keep them inert)
vi.mock("@/api/ai", () => ({
  useAIStatus: () => ({ data: { enabled: false } }),
  useSuggestTemplateIdentity: () => ({ mutateAsync: vi.fn(), isPending: false }),
}));

import { TemplateEditorPage } from "@/pages/TemplateEditorPage";

const DEVICE_DRIVER = {
  id: "d1",
  name: "Junos Driver",
  description: "fw",
  connection_type: "Management",
  filename: "junos.zip",
  size_bytes: 1024,
  sha256: "abc",
  uploaded_by: "admin",
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

const HYPERVISOR_DRIVER = {
  id: "d2",
  name: "Proxmox Recipe",
  description: "hypervisor recipe",
  connection_type: "Hypervisor",
  filename: "proxmox.tar.gz",
  size_bytes: 2048,
  sha256: "def",
  uploaded_by: "admin",
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

const HYPERVISOR = {
  id: "h1",
  name: "Proxmox Lab",
  description: null,
  endpoint: "https://proxmox.example.local:8006",
  hypervisor_type: "proxmox",
  secret_id: "s1",
  enabled: true,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
  modified_by: null,
};

function selectSave() {
  return screen.getByRole("button", { name: "Save" });
}

describe("TemplateEditorPage (create flow)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockUseTemplate.mockReturnValue({ data: undefined, isLoading: false });
    mockUseDrivers.mockReturnValue({ data: [DEVICE_DRIVER, HYPERVISOR_DRIVER] });
    mockUseHypervisors.mockReturnValue({ data: [HYPERVISOR] });
  });

  it("offers a Dynamic (Hypervisor) type option", () => {
    render(<TemplateEditorPage />);
    const select = screen.getByLabelText("Type");
    const options = Array.from(select.querySelectorAll("option")).map((o) => o.textContent);
    expect(options).toContain("Dynamic (Hypervisor)");
  });

  it("device type: driver dropdown excludes Hypervisor-type drivers", () => {
    render(<TemplateEditorPage />);
    // Default type is "device"
    const driverSelect = screen.getByLabelText("Driver");
    const options = Array.from(driverSelect.querySelectorAll("option")).map((o) => o.textContent);
    expect(options.some((o) => o?.includes("Junos Driver"))).toBe(true);
    expect(options.some((o) => o?.includes("Proxmox Recipe"))).toBe(false);
  });

  it("device flow: Save still requires a driver (unchanged behavior)", () => {
    render(<TemplateEditorPage />);
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "My Device Tmpl" } });
    // Save is disabled until vendor/model are filled (identityValid), matching
    // pre-existing device behavior; fill those so the driver check is reached.
    fireEvent.change(screen.getByLabelText("Vendor *"), { target: { value: "Cisco" } });
    fireEvent.change(screen.getByLabelText("Model *"), { target: { value: "Catalyst" } });
    fireEvent.click(selectSave());
    expect(mockToastError).toHaveBeenCalledWith("Device templates must have a driver");
  });

  it("port type: no driver or hypervisor selector, no identity requirement", () => {
    render(<TemplateEditorPage />);
    fireEvent.change(screen.getByLabelText("Type"), { target: { value: "port" } });
    expect(screen.queryByLabelText("Driver")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Recipe Driver")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Hypervisor")).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "My Port Tmpl" } });
    fireEvent.click(selectSave());
    // No driver/identity errors block a port save.
    expect(mockToastError).not.toHaveBeenCalled();
    expect(mockCreateTemplate.mutateAsync).toHaveBeenCalledWith(
      expect.objectContaining({ template_type: "port", driver_id: null, hypervisor_id: null }),
    );
  });

  it("dynamic type: shows a Recipe Driver selector filtered to Hypervisor-type drivers", () => {
    render(<TemplateEditorPage />);
    fireEvent.change(screen.getByLabelText("Type"), { target: { value: "dynamic" } });
    const driverSelect = screen.getByLabelText("Recipe Driver");
    const options = Array.from(driverSelect.querySelectorAll("option")).map((o) => o.textContent);
    expect(options.some((o) => o?.includes("Proxmox Recipe"))).toBe(true);
    expect(options.some((o) => o?.includes("Junos Driver"))).toBe(false);
  });

  it("dynamic type: shows a Hypervisor selector", () => {
    render(<TemplateEditorPage />);
    fireEvent.change(screen.getByLabelText("Type"), { target: { value: "dynamic" } });
    const hvSelect = screen.getByLabelText("Hypervisor");
    const options = Array.from(hvSelect.querySelectorAll("option")).map((o) => o.textContent);
    expect(options.some((o) => o?.includes("Proxmox Lab"))).toBe(true);
  });

  it("dynamic type: does not require vendor/model (identity)", () => {
    render(<TemplateEditorPage />);
    fireEvent.change(screen.getByLabelText("Type"), { target: { value: "dynamic" } });
    expect(screen.queryByText("Hardware Identity")).not.toBeInTheDocument();
  });

  it("dynamic type: Save requires a recipe driver", () => {
    render(<TemplateEditorPage />);
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "My Dynamic Tmpl" } });
    fireEvent.change(screen.getByLabelText("Type"), { target: { value: "dynamic" } });
    fireEvent.click(selectSave());
    expect(mockToastError).toHaveBeenCalledWith("Dynamic templates must have a recipe driver");
  });

  it("dynamic type: Save requires a hypervisor once a driver is chosen", () => {
    render(<TemplateEditorPage />);
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "My Dynamic Tmpl" } });
    fireEvent.change(screen.getByLabelText("Type"), { target: { value: "dynamic" } });
    fireEvent.change(screen.getByLabelText("Recipe Driver"), { target: { value: "d2" } });
    fireEvent.click(selectSave());
    expect(mockToastError).toHaveBeenCalledWith("Dynamic templates must have a hypervisor");
  });

  it("dynamic type: a fully filled form saves with driver_id and hypervisor_id", () => {
    render(<TemplateEditorPage />);
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "My Dynamic Tmpl" } });
    fireEvent.change(screen.getByLabelText("Type"), { target: { value: "dynamic" } });
    fireEvent.change(screen.getByLabelText("Recipe Driver"), { target: { value: "d2" } });
    fireEvent.change(screen.getByLabelText("Hypervisor"), { target: { value: "h1" } });
    fireEvent.click(selectSave());
    expect(mockToastError).not.toHaveBeenCalled();
    expect(mockCreateTemplate.mutateAsync).toHaveBeenCalledWith(
      expect.objectContaining({
        template_type: "dynamic",
        driver_id: "d2",
        hypervisor_id: "h1",
      }),
    );
  });

  it("switching from dynamic back to device clears the driver and hypervisor selection", () => {
    render(<TemplateEditorPage />);
    fireEvent.change(screen.getByLabelText("Type"), { target: { value: "dynamic" } });
    fireEvent.change(screen.getByLabelText("Recipe Driver"), { target: { value: "d2" } });
    fireEvent.change(screen.getByLabelText("Hypervisor"), { target: { value: "h1" } });
    fireEvent.change(screen.getByLabelText("Type"), { target: { value: "device" } });
    // The Driver selector reappears with no selection carried over.
    const driverSelect = screen.getByLabelText("Driver") as HTMLSelectElement;
    expect(driverSelect.value).toBe("");
    expect(screen.queryByLabelText("Hypervisor")).not.toBeInTheDocument();
  });
});
