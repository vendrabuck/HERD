import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, beforeAll, beforeEach } from "vitest";

// Mock HTMLDialogElement methods (jsdom has no native <dialog> support)
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

// Mock hypervisor hooks
const mockUsePaginatedHypervisors = vi.fn();
const mockCreateHypervisor = { mutateAsync: vi.fn(), isPending: false };
const mockUpdateHypervisor = { mutateAsync: vi.fn(), isPending: false };
const mockDeleteHypervisor = { mutateAsync: vi.fn(), isPending: false };

vi.mock("@/api/hypervisors", () => ({
  usePaginatedHypervisors: (...args: unknown[]) => mockUsePaginatedHypervisors(...args),
  useCreateHypervisor: () => mockCreateHypervisor,
  useUpdateHypervisor: () => mockUpdateHypervisor,
  useDeleteHypervisor: () => mockDeleteHypervisor,
}));

// Mock secrets hook (populates the secret_id picker)
const mockUseSecrets = vi.fn();
vi.mock("@/api/secrets", () => ({
  useSecrets: () => mockUseSecrets(),
}));

import { HypervisorsPage } from "@/pages/admin/HypervisorsPage";

const SAMPLE_HYPERVISORS = [
  {
    id: "h1",
    name: "Proxmox Lab",
    description: "lab cluster",
    endpoint: "https://proxmox.example.local:8006",
    hypervisor_type: "proxmox",
    secret_id: "s1",
    enabled: true,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    modified_by: null,
  },
  {
    id: "h2",
    name: "vSphere Lab",
    description: null,
    endpoint: "https://vcenter.example.local",
    hypervisor_type: "vsphere",
    secret_id: "s2",
    enabled: false,
    created_at: "2026-01-02T00:00:00Z",
    updated_at: "2026-01-02T00:00:00Z",
    modified_by: null,
  },
];

const SAMPLE_SECRETS = [
  { id: "s1", name: "proxmox-cred", type: "password", description: null, key_version: 1, created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z" },
  { id: "s2", name: "vsphere-cred", type: "password", description: null, key_version: 1, created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z" },
];

describe("HypervisorsPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockUsePaginatedHypervisors.mockReturnValue({
      data: { items: SAMPLE_HYPERVISORS, total: 2 },
      isLoading: false,
    });
    mockUseSecrets.mockReturnValue({ data: SAMPLE_SECRETS });
  });

  it("renders hypervisors table with data", () => {
    render(<HypervisorsPage />);
    expect(screen.getByText("Proxmox Lab")).toBeInTheDocument();
    expect(screen.getByText("vSphere Lab")).toBeInTheDocument();
    expect(screen.getByText("proxmox")).toBeInTheDocument();
    expect(screen.getByText("vsphere")).toBeInTheDocument();
  });

  it("shows loading state", () => {
    mockUsePaginatedHypervisors.mockReturnValue({ data: undefined, isLoading: true });
    render(<HypervisorsPage />);
    expect(screen.getByText("Loading hypervisors...")).toBeInTheDocument();
  });

  it("shows empty state", () => {
    mockUsePaginatedHypervisors.mockReturnValue({
      data: { items: [], total: 0 },
      isLoading: false,
    });
    render(<HypervisorsPage />);
    expect(screen.getByText("No hypervisors found")).toBeInTheDocument();
  });

  it("register button opens the create modal", () => {
    const showModalSpy = vi.fn();
    HTMLDialogElement.prototype.showModal = showModalSpy;
    render(<HypervisorsPage />);
    fireEvent.click(screen.getByRole("button", { name: "Register Hypervisor" }));
    expect(showModalSpy).toHaveBeenCalled();
  });

  it("create validates name required", () => {
    render(<HypervisorsPage />);
    fireEvent.click(screen.getByRole("button", { name: "Register Hypervisor" }));
    const dialog = document.querySelectorAll("dialog")[0];
    const submitBtn = Array.from(dialog.querySelectorAll("button")).find(
      (b) => b.textContent === "Register",
    )!;
    fireEvent.click(submitBtn);
    expect(mockToastError).toHaveBeenCalledWith("Name is required");
  });

  it("create validates endpoint required", () => {
    render(<HypervisorsPage />);
    fireEvent.click(screen.getByRole("button", { name: "Register Hypervisor" }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "New HV" } });
    const dialog = document.querySelectorAll("dialog")[0];
    const submitBtn = Array.from(dialog.querySelectorAll("button")).find(
      (b) => b.textContent === "Register",
    )!;
    fireEvent.click(submitBtn);
    expect(mockToastError).toHaveBeenCalledWith("Endpoint is required");
  });

  it("create validates hypervisor type required", () => {
    render(<HypervisorsPage />);
    fireEvent.click(screen.getByRole("button", { name: "Register Hypervisor" }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "New HV" } });
    fireEvent.change(screen.getByLabelText("Endpoint"), {
      target: { value: "https://hv.example.local" },
    });
    const dialog = document.querySelectorAll("dialog")[0];
    const submitBtn = Array.from(dialog.querySelectorAll("button")).find(
      (b) => b.textContent === "Register",
    )!;
    fireEvent.click(submitBtn);
    expect(mockToastError).toHaveBeenCalledWith("Hypervisor type is required");
  });

  it("create validates secret required", () => {
    render(<HypervisorsPage />);
    fireEvent.click(screen.getByRole("button", { name: "Register Hypervisor" }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "New HV" } });
    fireEvent.change(screen.getByLabelText("Endpoint"), {
      target: { value: "https://hv.example.local" },
    });
    fireEvent.change(screen.getByLabelText("Hypervisor Type"), {
      target: { value: "proxmox" },
    });
    const dialog = document.querySelectorAll("dialog")[0];
    const submitBtn = Array.from(dialog.querySelectorAll("button")).find(
      (b) => b.textContent === "Register",
    )!;
    fireEvent.click(submitBtn);
    expect(mockToastError).toHaveBeenCalledWith("A secret is required");
  });

  it("create submits a valid form", async () => {
    render(<HypervisorsPage />);
    fireEvent.click(screen.getByRole("button", { name: "Register Hypervisor" }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "New HV" } });
    fireEvent.change(screen.getByLabelText("Endpoint"), {
      target: { value: "https://hv.example.local" },
    });
    fireEvent.change(screen.getByLabelText("Hypervisor Type"), {
      target: { value: "proxmox" },
    });
    fireEvent.change(screen.getByLabelText("Secret"), { target: { value: "s1" } });
    const dialog = document.querySelectorAll("dialog")[0];
    const submitBtn = Array.from(dialog.querySelectorAll("button")).find(
      (b) => b.textContent === "Register",
    )!;
    fireEvent.click(submitBtn);
    expect(mockCreateHypervisor.mutateAsync).toHaveBeenCalledWith(
      expect.objectContaining({
        name: "New HV",
        endpoint: "https://hv.example.local",
        hypervisor_type: "proxmox",
        secret_id: "s1",
      }),
    );
  });

  it("edit button opens the edit modal pre-filled", () => {
    render(<HypervisorsPage />);
    const editButtons = screen.getAllByText("Edit");
    fireEvent.click(editButtons[0]);
    expect(screen.getByText("Edit Hypervisor")).toBeInTheDocument();
    expect(screen.getByDisplayValue("Proxmox Lab")).toBeInTheDocument();
    expect(screen.getByDisplayValue("https://proxmox.example.local:8006")).toBeInTheDocument();
  });

  it("delete button shows confirm dialog", () => {
    render(<HypervisorsPage />);
    const deleteButtons = screen.getAllByText("Delete");
    fireEvent.click(deleteButtons[0]);
    expect(screen.getByText("Delete Hypervisor")).toBeInTheDocument();
    expect(screen.getByText(/Delete "Proxmox Lab"\?/)).toBeInTheDocument();
  });

  // Mutation-failure toast surfacing. inventory (like acl) shapes error
  // detail as either a string or, on a pydantic validation failure, a list
  // of error objects; only the string case may reach toast.error directly
  // (a list passed to it renders as a raw object child and throws inside the
  // Toaster, which sits outside App.tsx's ErrorBoundary and blanks the app).
  // Mirrors GrantsPage.test.tsx's create-failure/list-shaped-detail coverage.

  it("create failure surfaces the server detail message", async () => {
    mockCreateHypervisor.mutateAsync.mockRejectedValueOnce({
      response: { data: { detail: "This hypervisor already exists" } },
    });
    render(<HypervisorsPage />);
    fireEvent.click(screen.getByRole("button", { name: "Register Hypervisor" }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "New HV" } });
    fireEvent.change(screen.getByLabelText("Endpoint"), {
      target: { value: "https://hv.example.local" },
    });
    fireEvent.change(screen.getByLabelText("Hypervisor Type"), {
      target: { value: "proxmox" },
    });
    fireEvent.change(screen.getByLabelText("Secret"), { target: { value: "s1" } });
    const dialog = document.querySelectorAll("dialog")[0];
    const submitBtn = Array.from(dialog.querySelectorAll("button")).find(
      (b) => b.textContent === "Register",
    )!;
    fireEvent.click(submitBtn);

    await waitFor(() =>
      expect(mockToastError).toHaveBeenCalledWith("This hypervisor already exists"),
    );
  });

  it("create failure with a list-shaped detail falls back to a generic message", async () => {
    mockCreateHypervisor.mutateAsync.mockRejectedValueOnce({
      response: {
        data: {
          detail: [{ type: "uuid_parsing", loc: ["body", "secret_id"], msg: "bad uuid" }],
        },
      },
    });
    render(<HypervisorsPage />);
    fireEvent.click(screen.getByRole("button", { name: "Register Hypervisor" }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "New HV" } });
    fireEvent.change(screen.getByLabelText("Endpoint"), {
      target: { value: "https://hv.example.local" },
    });
    fireEvent.change(screen.getByLabelText("Hypervisor Type"), {
      target: { value: "proxmox" },
    });
    fireEvent.change(screen.getByLabelText("Secret"), { target: { value: "s1" } });
    const dialog = document.querySelectorAll("dialog")[0];
    const submitBtn = Array.from(dialog.querySelectorAll("button")).find(
      (b) => b.textContent === "Register",
    )!;
    fireEvent.click(submitBtn);

    await waitFor(() =>
      expect(mockToastError).toHaveBeenCalledWith("Failed to register hypervisor"),
    );
    // The raw array must never reach toast.error.
    expect(mockToastError).not.toHaveBeenCalledWith(expect.any(Array));
  });

  it("update failure surfaces the server detail message", async () => {
    mockUpdateHypervisor.mutateAsync.mockRejectedValueOnce({
      response: { data: { detail: "endpoint is already registered" } },
    });
    render(<HypervisorsPage />);
    const editButtons = screen.getAllByText("Edit");
    fireEvent.click(editButtons[0]);
    const dialog = document.querySelectorAll("dialog")[0];
    const submitBtn = Array.from(dialog.querySelectorAll("button")).find(
      (b) => b.textContent === "Save",
    )!;
    fireEvent.click(submitBtn);

    await waitFor(() =>
      expect(mockToastError).toHaveBeenCalledWith("endpoint is already registered"),
    );
  });

  it("delete failure surfaces the backend's 409 detail message", async () => {
    mockDeleteHypervisor.mutateAsync.mockRejectedValueOnce({
      response: {
        data: { detail: "Cannot delete hypervisor: templates still reference it" },
      },
    });
    render(<HypervisorsPage />);
    const deleteButtons = screen.getAllByText("Delete");
    fireEvent.click(deleteButtons[0]);

    const confirmDialog = document.querySelectorAll("dialog")[1];
    const confirmBtn = Array.from(confirmDialog.querySelectorAll("button")).find(
      (b) => b.textContent === "Delete",
    )!;
    fireEvent.click(confirmBtn);

    await waitFor(() =>
      expect(mockToastError).toHaveBeenCalledWith(
        "Cannot delete hypervisor: templates still reference it",
      ),
    );
  });

  // Issue #456 orphan rendering: a deleted secret leaves a dangling
  // secret_id; the page must say so explicitly rather than show a bare
  // truncated id (list) or an empty dropdown (edit form).

  const ORPHANED = {
    ...SAMPLE_HYPERVISORS[0],
    id: "h3",
    name: "Orphaned HV",
    secret_id: "deadbeef-0000-4000-8000-000000000000",
  };

  it("list labels an orphaned secret reference explicitly", () => {
    mockUsePaginatedHypervisors.mockReturnValue({
      data: { items: [ORPHANED], total: 1 },
      isLoading: false,
    });
    render(<HypervisorsPage />);
    expect(screen.getByText("deleted secret deadbeef")).toBeInTheDocument();
  });

  it("list keeps the neutral truncated id while secrets are still loading", () => {
    mockUsePaginatedHypervisors.mockReturnValue({
      data: { items: [ORPHANED], total: 1 },
      isLoading: false,
    });
    mockUseSecrets.mockReturnValue({ data: undefined });
    render(<HypervisorsPage />);
    expect(screen.getByText("deadbeef...")).toBeInTheDocument();
    expect(screen.queryByText(/deleted secret/)).not.toBeInTheDocument();
  });

  it("edit form selects the orphaned option and warns", () => {
    mockUsePaginatedHypervisors.mockReturnValue({
      data: { items: [ORPHANED], total: 1 },
      isLoading: false,
    });
    render(<HypervisorsPage />);
    fireEvent.click(screen.getAllByText("Edit")[0]);
    const select = screen.getByLabelText("Secret") as HTMLSelectElement;
    expect(select.value).toBe("deadbeef-0000-4000-8000-000000000000");
    expect(
      screen.getByText(/references a secret that no longer exists/),
    ).toBeInTheDocument();
    // Selecting a live secret clears the warning.
    fireEvent.change(select, { target: { value: "s1" } });
    expect(
      screen.queryByText(/references a secret that no longer exists/),
    ).not.toBeInTheDocument();
  });
});
