import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi, beforeAll, beforeEach } from "vitest";

// Mock HTMLDialogElement methods
beforeAll(() => {
  HTMLDialogElement.prototype.showModal = vi.fn();
  HTMLDialogElement.prototype.close = vi.fn();
});

// Mock react-router-dom
vi.mock("react-router-dom", () => ({
  useNavigate: () => vi.fn(),
}));

// Mock react-hot-toast
const mockToastError = vi.fn();
vi.mock("react-hot-toast", () => ({
  default: {
    error: (...args: unknown[]) => mockToastError(...args),
    success: vi.fn(),
  },
}));

// Mock authStore
const mockUser = { id: "1", email: "admin@test.com", role: "admin", username: "admin" };
vi.mock("@/stores/authStore", () => ({
  useAuthStore: (selector: (s: { user: typeof mockUser }) => unknown) =>
    selector({ user: mockUser }),
}));

// Mock driver hooks
const mockUsePaginatedDrivers = vi.fn();
const mockCreateDriver = { mutateAsync: vi.fn(), isPending: false };
const mockDeleteDriver = { mutateAsync: vi.fn(), isPending: false };
const mockDownloadDriver = { mutateAsync: vi.fn(), isPending: false };

vi.mock("@/api/drivers", () => ({
  usePaginatedDrivers: (...args: unknown[]) => mockUsePaginatedDrivers(...args),
  useCreateDriver: () => mockCreateDriver,
  useDeleteDriver: () => mockDeleteDriver,
  useDownloadDriver: () => mockDownloadDriver,
}));

// AI status drives the conditional Draft with AI button (issue #28). Default
// to the feature being off; individual tests flip it on.
const mockUseAIStatus = vi.fn();
vi.mock("@/api/ai", () => ({
  useAIStatus: () => mockUseAIStatus(),
}));

vi.mock("@/api/recipes", () => ({
  useDraftRecipe: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useRefineRecipeDraft: () => ({ mutateAsync: vi.fn(), isPending: false }),
  recipePackageFile: vi.fn(),
}));

import { DriversPage } from "@/pages/admin/DriversPage";

const SAMPLE_DRIVERS = [
  {
    id: "d1",
    name: "Junos Driver",
    description: "Firewall management",
    connection_type: "Management",
    filename: "junos.zip",
    size_bytes: 1024,
    sha256: "abc",
    uploaded_by: "admin",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  },
  {
    id: "d2",
    name: "Switch Driver",
    description: "L2 switch",
    connection_type: "Layer 2 Switch",
    filename: "switch.tar.gz",
    size_bytes: 2048000,
    sha256: "def",
    uploaded_by: "admin",
    created_at: "2026-01-02T00:00:00Z",
    updated_at: "2026-01-02T00:00:00Z",
  },
];

describe("DriversPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockUsePaginatedDrivers.mockReturnValue({
      data: { items: SAMPLE_DRIVERS, total: 2 },
      isLoading: false,
    });
    mockUseAIStatus.mockReturnValue({
      data: { enabled: true, recipe_authoring: false },
    });
  });

  it("renders drivers table with data", () => {
    render(<DriversPage />);
    expect(screen.getByText("Junos Driver")).toBeInTheDocument();
    expect(screen.getByText("Switch Driver")).toBeInTheDocument();
    // "Management" appears in both table and select option
    const mgmtElements = screen.getAllByText("Management");
    expect(mgmtElements.length).toBeGreaterThanOrEqual(1);
    // "Layer 2 Switch" appears in both table and select option
    const l2Elements = screen.getAllByText("Layer 2 Switch");
    expect(l2Elements.length).toBeGreaterThanOrEqual(1);
  });

  it("shows loading state", () => {
    mockUsePaginatedDrivers.mockReturnValue({
      data: undefined,
      isLoading: true,
    });
    render(<DriversPage />);
    expect(screen.getByText("Loading drivers...")).toBeInTheDocument();
  });

  it("shows empty state", () => {
    mockUsePaginatedDrivers.mockReturnValue({
      data: { items: [], total: 0 },
      isLoading: false,
    });
    render(<DriversPage />);
    expect(screen.getByText("No drivers found")).toBeInTheDocument();
  });

  it("upload button opens modal", () => {
    const showModalSpy = vi.fn();
    HTMLDialogElement.prototype.showModal = showModalSpy;
    render(<DriversPage />);
    // Click the Upload Driver button in the header
    fireEvent.click(screen.getByRole("button", { name: "Upload Driver" }));
    // showModal should have been called (modal was opened)
    expect(showModalSpy).toHaveBeenCalled();
  });

  it("upload validates name required", () => {
    render(<DriversPage />);
    // Open upload modal via the header button
    const headerBtn = screen.getByRole("button", { name: "Upload Driver" });
    fireEvent.click(headerBtn);
    // Find the submit button inside the upload modal (first dialog)
    const dialogs = document.querySelectorAll("dialog");
    const uploadDialog = dialogs[0];
    const submitBtn = Array.from(uploadDialog.querySelectorAll("button")).find(
      (b) => b.textContent === "Upload"
    )!;
    fireEvent.click(submitBtn);
    expect(mockToastError).toHaveBeenCalledWith("Name is required");
  });

  it("upload validates connection type required", () => {
    render(<DriversPage />);
    fireEvent.click(screen.getByRole("button", { name: "Upload Driver" }));
    fireEvent.change(screen.getByLabelText("Name"), {
      target: { value: "Test Driver" },
    });
    const uploadDialog = document.querySelectorAll("dialog")[0];
    const submitBtn = Array.from(uploadDialog.querySelectorAll("button")).find(
      (b) => b.textContent === "Upload"
    )!;
    fireEvent.click(submitBtn);
    expect(mockToastError).toHaveBeenCalledWith("Connection type is required");
  });

  it("upload validates file required", () => {
    render(<DriversPage />);
    fireEvent.click(screen.getByRole("button", { name: "Upload Driver" }));
    fireEvent.change(screen.getByLabelText("Name"), {
      target: { value: "Test Driver" },
    });
    fireEvent.change(screen.getByLabelText("Connection Type"), {
      target: { value: "Management" },
    });
    const uploadDialog = document.querySelectorAll("dialog")[0];
    const submitBtn = Array.from(uploadDialog.querySelectorAll("button")).find(
      (b) => b.textContent === "Upload"
    )!;
    fireEvent.click(submitBtn);
    expect(mockToastError).toHaveBeenCalledWith("File is required");
  });

  it("hides Draft with AI when recipe authoring is off", () => {
    render(<DriversPage />);
    expect(screen.queryByRole("button", { name: "Draft with AI" })).not.toBeInTheDocument();
  });

  it("hides Draft with AI when AI itself is unconfigured", () => {
    mockUseAIStatus.mockReturnValue({
      data: { enabled: false, recipe_authoring: true },
    });
    render(<DriversPage />);
    expect(screen.queryByRole("button", { name: "Draft with AI" })).not.toBeInTheDocument();
  });

  it("shows Draft with AI and opens the panel when the flag is on", () => {
    mockUseAIStatus.mockReturnValue({
      data: { enabled: true, recipe_authoring: true },
    });
    const showModalSpy = vi.fn();
    HTMLDialogElement.prototype.showModal = showModalSpy;
    render(<DriversPage />);
    const btn = screen.getByRole("button", { name: "Draft with AI" });
    fireEvent.click(btn);
    expect(showModalSpy).toHaveBeenCalled();
    expect(screen.getByText("Draft Recipe with AI")).toBeInTheDocument();
  });

  it("upload select offers the Hypervisor connection type", () => {
    render(<DriversPage />);
    fireEvent.click(screen.getByRole("button", { name: "Upload Driver" }));
    const select = screen.getByLabelText("Connection Type");
    const options = Array.from(select.querySelectorAll("option")).map((o) => o.textContent);
    expect(options).toContain("Hypervisor");
  });

  it("delete button shows confirm dialog", () => {
    render(<DriversPage />);
    const deleteButtons = screen.getAllByText("Delete");
    // Click Delete on first driver row
    fireEvent.click(deleteButtons[0]);
    expect(screen.getByText("Delete Driver")).toBeInTheDocument();
    expect(
      screen.getByText(/Are you sure you want to delete this driver/)
    ).toBeInTheDocument();
  });
});
