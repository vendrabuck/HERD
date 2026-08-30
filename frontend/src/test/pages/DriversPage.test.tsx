import { render, screen, fireEvent, waitFor } from "@testing-library/react";
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

  it("formats a large file size in MB rather than KB", () => {
    render(<DriversPage />);
    // SAMPLE_DRIVERS[1].size_bytes is 2048000, which is >= 1024*1024.
    expect(screen.getByText("2.0 MB")).toBeInTheDocument();
    // The smaller driver stays in KB.
    expect(screen.getByText("1.0 KB")).toBeInTheDocument();
  });

  it("uploads a driver with the full payload and closes the modal on success", async () => {
    mockCreateDriver.mutateAsync.mockResolvedValue({ id: "new-driver" });
    render(<DriversPage />);

    fireEvent.click(screen.getByRole("button", { name: "Upload Driver" }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "New Driver" } });
    fireEvent.change(screen.getByLabelText("Description"), {
      target: { value: "a description" },
    });
    fireEvent.change(screen.getByLabelText("Connection Type"), {
      target: { value: "Hypervisor" },
    });
    const file = new File(["zip bytes"], "new.zip", { type: "application/zip" });
    fireEvent.change(screen.getByLabelText("File (.zip or .tar.gz)"), {
      target: { files: [file] },
    });

    const uploadDialog = document.querySelectorAll("dialog")[0];
    const submitBtn = Array.from(uploadDialog.querySelectorAll("button")).find(
      (b) => b.textContent === "Upload"
    )!;
    fireEvent.click(submitBtn);

    await waitFor(() => expect(mockCreateDriver.mutateAsync).toHaveBeenCalled());
    expect(mockCreateDriver.mutateAsync).toHaveBeenCalledWith({
      name: "New Driver",
      description: "a description",
      connection_type: "Hypervisor",
      file,
    });
    expect(mockToastSuccess).toHaveBeenCalledWith("Driver uploaded");
    // The upload modal's fields reset (closeUploadModal's job); Name is
    // gone from the DOM only if the dialog fully unmounts, so instead check
    // the field cleared while remaining mounted.
    expect((screen.getByLabelText("Name") as HTMLInputElement).value).toBe("");
  });

  it("omits the description field when it is left blank", async () => {
    mockCreateDriver.mutateAsync.mockResolvedValue({ id: "new-driver" });
    render(<DriversPage />);

    fireEvent.click(screen.getByRole("button", { name: "Upload Driver" }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "New Driver" } });
    fireEvent.change(screen.getByLabelText("Connection Type"), {
      target: { value: "Management" },
    });
    const file = new File(["zip bytes"], "new.zip", { type: "application/zip" });
    fireEvent.change(screen.getByLabelText("File (.zip or .tar.gz)"), {
      target: { files: [file] },
    });
    const uploadDialog = document.querySelectorAll("dialog")[0];
    const submitBtn = Array.from(uploadDialog.querySelectorAll("button")).find(
      (b) => b.textContent === "Upload"
    )!;
    fireEvent.click(submitBtn);

    await waitFor(() => expect(mockCreateDriver.mutateAsync).toHaveBeenCalled());
    expect(mockCreateDriver.mutateAsync.mock.calls[0][0].description).toBeUndefined();
  });

  it("surfaces the server detail message when upload fails", async () => {
    mockCreateDriver.mutateAsync.mockRejectedValue({
      response: { data: { detail: "duplicate driver name" } },
    });
    render(<DriversPage />);

    fireEvent.click(screen.getByRole("button", { name: "Upload Driver" }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "New Driver" } });
    fireEvent.change(screen.getByLabelText("Connection Type"), {
      target: { value: "Management" },
    });
    fireEvent.change(screen.getByLabelText("File (.zip or .tar.gz)"), {
      target: { files: [new File(["x"], "n.zip")] },
    });
    const uploadDialog = document.querySelectorAll("dialog")[0];
    const submitBtn = Array.from(uploadDialog.querySelectorAll("button")).find(
      (b) => b.textContent === "Upload"
    )!;
    fireEvent.click(submitBtn);

    await waitFor(() =>
      expect(mockToastError).toHaveBeenCalledWith("duplicate driver name"),
    );
  });

  it("falls back to a generic message when the upload error has no detail", async () => {
    mockCreateDriver.mutateAsync.mockRejectedValue(new Error("network down"));
    render(<DriversPage />);

    fireEvent.click(screen.getByRole("button", { name: "Upload Driver" }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "New Driver" } });
    fireEvent.change(screen.getByLabelText("Connection Type"), {
      target: { value: "Management" },
    });
    fireEvent.change(screen.getByLabelText("File (.zip or .tar.gz)"), {
      target: { files: [new File(["x"], "n.zip")] },
    });
    const uploadDialog = document.querySelectorAll("dialog")[0];
    const submitBtn = Array.from(uploadDialog.querySelectorAll("button")).find(
      (b) => b.textContent === "Upload"
    )!;
    fireEvent.click(submitBtn);

    await waitFor(() =>
      expect(mockToastError).toHaveBeenCalledWith("Failed to upload driver"),
    );
  });

  it("cancel closes the upload modal without submitting", () => {
    render(<DriversPage />);
    fireEvent.click(screen.getByRole("button", { name: "Upload Driver" }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "abandoned" } });

    const uploadDialog = document.querySelectorAll("dialog")[0];
    const cancelBtn = Array.from(uploadDialog.querySelectorAll("button")).find(
      (b) => b.textContent === "Cancel"
    )!;
    fireEvent.click(cancelBtn);

    expect(mockCreateDriver.mutateAsync).not.toHaveBeenCalled();
    // Reopening shows the reset form, not the abandoned value.
    fireEvent.click(screen.getByRole("button", { name: "Upload Driver" }));
    expect((screen.getByLabelText("Name") as HTMLInputElement).value).toBe("");
  });

  describe("delete flow", () => {
    it("deletes the driver and toasts success", async () => {
      mockDeleteDriver.mutateAsync.mockResolvedValue(undefined);
      render(<DriversPage />);
      fireEvent.click(screen.getAllByText("Delete")[0]);

      const confirmDialog = document.querySelectorAll("dialog")[2];
      const confirmBtn = Array.from(confirmDialog.querySelectorAll("button")).find(
        (b) => b.textContent === "Delete"
      )!;
      fireEvent.click(confirmBtn);

      await waitFor(() => expect(mockDeleteDriver.mutateAsync).toHaveBeenCalledWith("d1"));
      expect(mockToastSuccess).toHaveBeenCalledWith("Driver deleted");
    });

    it("surfaces the server detail message when delete fails", async () => {
      mockDeleteDriver.mutateAsync.mockRejectedValue({
        response: { data: { detail: "referenced by a template" } },
      });
      render(<DriversPage />);
      fireEvent.click(screen.getAllByText("Delete")[0]);

      const confirmDialog = document.querySelectorAll("dialog")[2];
      const confirmBtn = Array.from(confirmDialog.querySelectorAll("button")).find(
        (b) => b.textContent === "Delete"
      )!;
      fireEvent.click(confirmBtn);

      await waitFor(() =>
        expect(mockToastError).toHaveBeenCalledWith("referenced by a template"),
      );
    });

    it("cancelling the delete confirmation issues no delete call", () => {
      render(<DriversPage />);
      fireEvent.click(screen.getAllByText("Delete")[0]);

      const confirmDialog = document.querySelectorAll("dialog")[2];
      const cancelBtn = Array.from(confirmDialog.querySelectorAll("button")).find(
        (b) => b.textContent === "Cancel"
      )!;
      fireEvent.click(cancelBtn);

      expect(mockDeleteDriver.mutateAsync).not.toHaveBeenCalled();
    });
  });

  describe("download flow", () => {
    it("downloads a driver by creating and clicking an object URL anchor", async () => {
      const blob = new Blob(["zip bytes"], { type: "application/zip" });
      mockDownloadDriver.mutateAsync.mockResolvedValue(blob);
      const createObjectURL = vi.fn(() => "blob:mock-url");
      const revokeObjectURL = vi.fn();
      vi.stubGlobal("URL", { ...URL, createObjectURL, revokeObjectURL });

      const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});

      render(<DriversPage />);
      fireEvent.click(screen.getAllByText("Download")[0]);

      await waitFor(() => expect(mockDownloadDriver.mutateAsync).toHaveBeenCalledWith("d1"));
      expect(createObjectURL).toHaveBeenCalledWith(blob);
      expect(clickSpy).toHaveBeenCalled();
      expect(revokeObjectURL).toHaveBeenCalledWith("blob:mock-url");

      clickSpy.mockRestore();
      vi.unstubAllGlobals();
    });

    it("toasts an error when the download fails", async () => {
      mockDownloadDriver.mutateAsync.mockRejectedValue(new Error("boom"));
      render(<DriversPage />);
      fireEvent.click(screen.getAllByText("Download")[0]);

      await waitFor(() =>
        expect(mockToastError).toHaveBeenCalledWith("Failed to download driver"),
      );
    });
  });
});
