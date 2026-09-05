import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";

// Mock react-router-dom
const mockNavigate = vi.fn();
vi.mock("react-router-dom", () => ({
  useNavigate: () => mockNavigate,
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
let mockUser: { id: string; role: string } | null = { id: "user-1", role: "user" };
vi.mock("@/stores/authStore", () => ({
  useAuthStore: (selector: (s: { user: typeof mockUser }) => unknown) =>
    selector({ user: mockUser }),
}));

// Mock topologyTemplates hooks
const mockUseTopologyTemplates = vi.fn();
const mockUseTopologyTemplate = vi.fn();
const mockDeleteTemplate = { mutateAsync: vi.fn(), isPending: false };
const mockInstantiate = { mutateAsync: vi.fn(), isPending: false };
vi.mock("@/api/topologyTemplates", () => ({
  useTopologyTemplates: (...args: unknown[]) => mockUseTopologyTemplates(...args),
  useDeleteTopologyTemplate: () => mockDeleteTemplate,
  useTopologyTemplate: (...args: unknown[]) => mockUseTopologyTemplate(...args),
  useInstantiateTemplate: () => mockInstantiate,
}));

// Mock devices hook
const mockUseDevices = vi.fn();
vi.mock("@/api/inventory", () => ({
  useAllDevices: (...args: unknown[]) => mockUseDevices(...args),
}));

import { TopologyTemplatesPage } from "@/pages/TopologyTemplatesPage";
import type { TopologyTemplate, TopologyTemplateDetail } from "@/api/topologyTemplates";

const TEMPLATE_MINE: TopologyTemplate = {
  id: "tmpl-mine",
  name: "Spine Leaf",
  description: "2 spine, 2 leaf",
  created_by: "user-1",
  owner_name: "Me",
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

const TEMPLATE_OTHER: TopologyTemplate = {
  id: "tmpl-other",
  name: "Simple Router Pair",
  description: null,
  created_by: "user-2",
  owner_name: "Someone Else",
  created_at: "2026-01-02T00:00:00Z",
  updated_at: "2026-01-02T00:00:00Z",
};

const TEMPLATE_DETAIL: TopologyTemplateDetail = {
  ...TEMPLATE_MINE,
  canvas_data: {
    nodes: [
      { id: "n1", data: { device: { role: "spine" } } },
      { id: "n2", data: { device: { role: "leaf" } } },
      { id: "n3", data: { label: "no role" } },
    ],
    edges: [],
  },
};

function renderPage() {
  return render(<TopologyTemplatesPage />);
}

beforeEach(() => {
  vi.clearAllMocks();
  mockUser = { id: "user-1", role: "user" };
  mockDeleteTemplate.mutateAsync.mockResolvedValue(undefined);
  mockInstantiate.mutateAsync.mockResolvedValue({ id: "new-topo" });
  mockUseTopologyTemplates.mockReturnValue({
    data: { items: [TEMPLATE_MINE, TEMPLATE_OTHER], total: 2, skip: 0, limit: 50 },
    isLoading: false,
    isError: false,
  });
  mockUseTopologyTemplate.mockReturnValue({ data: TEMPLATE_DETAIL, isLoading: false });
  mockUseDevices.mockReturnValue({
    data: [
      { id: "dev-1", name: "spine-a", template_name: "Cisco Spine" },
      { id: "dev-2", name: "leaf-a", template_name: "Cisco Leaf" },
    ],
  });
});

describe("TopologyTemplatesPage loading/error/empty states", () => {
  it("shows the loading status text while fetching", () => {
    mockUseTopologyTemplates.mockReturnValue({ data: undefined, isLoading: true, isError: false });
    renderPage();
    expect(screen.getByRole("status")).toHaveTextContent("Loading templates...");
  });

  it("shows the exact error text on a failed fetch", () => {
    mockUseTopologyTemplates.mockReturnValue({ data: undefined, isLoading: false, isError: true });
    renderPage();
    expect(screen.getByText("Failed to load templates")).toBeInTheDocument();
  });

  it("shows the exact empty-state text with the how-to-create hint", () => {
    mockUseTopologyTemplates.mockReturnValue({
      data: { items: [], total: 0, skip: 0, limit: 50 },
      isLoading: false,
      isError: false,
    });
    renderPage();
    expect(
      screen.getByText('No templates yet. Open a topology and click "Save as Template" to create one.'),
    ).toBeInTheDocument();
  });
});

describe("TopologyTemplatesPage list rendering and row permissions", () => {
  it("renders rows with name, owner, description, and the count", () => {
    renderPage();
    expect(screen.getByText("Topology Templates")).toBeInTheDocument();
    expect(screen.getByText("(2)")).toBeInTheDocument();
    expect(screen.getByText("Spine Leaf")).toBeInTheDocument();
    expect(screen.getByText("2 spine, 2 leaf")).toBeInTheDocument();
    expect(screen.getByText("Simple Router Pair")).toBeInTheDocument();
  });

  it("a non-admin sees Delete only on their own template row", () => {
    renderPage();
    const rows = screen.getAllByRole("row").slice(1);
    const mineRow = rows.find((r) => r.textContent?.includes("Spine Leaf"))!;
    const otherRow = rows.find((r) => r.textContent?.includes("Simple Router Pair"))!;
    expect(within(mineRow).getByText("Delete")).toBeInTheDocument();
    expect(within(otherRow).queryByText("Delete")).not.toBeInTheDocument();
  });

  it("an admin sees Delete on every row", () => {
    mockUser = { id: "user-1", role: "admin" };
    renderPage();
    const rows = screen.getAllByRole("row").slice(1);
    const otherRow = rows.find((r) => r.textContent?.includes("Simple Router Pair"))!;
    expect(within(otherRow).getByText("Delete")).toBeInTheDocument();
  });

  it("every row shows a Use button regardless of ownership", () => {
    renderPage();
    expect(screen.getAllByText("Use")).toHaveLength(2);
  });
});

describe("TopologyTemplatesPage delete flow", () => {
  it("confirming delete calls the mutation with the row id", async () => {
    renderPage();
    const rows = screen.getAllByRole("row").slice(1);
    const mineRow = rows.find((r) => r.textContent?.includes("Spine Leaf"))!;
    fireEvent.click(within(mineRow).getByText("Delete"));

    expect(screen.getByText("Delete template?")).toBeInTheDocument();
    const dialog = screen.getByText("Delete template?").closest("dialog")!;
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete" }));

    await waitFor(() => expect(mockDeleteTemplate.mutateAsync).toHaveBeenCalledWith("tmpl-mine"));
  });

  it("cancel does not call delete", () => {
    renderPage();
    const rows = screen.getAllByRole("row").slice(1);
    const mineRow = rows.find((r) => r.textContent?.includes("Spine Leaf"))!;
    fireEvent.click(within(mineRow).getByText("Delete"));
    const dialog = screen.getByText("Delete template?").closest("dialog")!;
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(mockDeleteTemplate.mutateAsync).not.toHaveBeenCalled();
  });
});

describe("TopologyTemplatesPage instantiate dialog", () => {
  it("derives unique roles from canvas_data nodes, filtering out nodes with no role", () => {
    renderPage();
    fireEvent.click(screen.getAllByText("Use")[0]);
    expect(screen.getByText("spine")).toBeInTheDocument();
    expect(screen.getByText("leaf")).toBeInTheDocument();
    // Only 2 role rows: the roleless third node contributes nothing.
    expect(screen.getAllByRole("combobox")).toHaveLength(2);
  });

  it("shows a no-roles hint when the template's canvas has none", () => {
    mockUseTopologyTemplate.mockReturnValue({
      data: { ...TEMPLATE_DETAIL, canvas_data: { nodes: [], edges: [] } },
      isLoading: false,
    });
    renderPage();
    fireEvent.click(screen.getAllByText("Use")[0]);
    expect(screen.getByText("This template has no roles to assign.")).toBeInTheDocument();
  });

  it("blocks submit with a toast when a role has no device assigned", async () => {
    renderPage();
    fireEvent.click(screen.getAllByText("Use")[0]);
    fireEvent.change(screen.getByLabelText("New topology name"), { target: { value: "Lab 1" } });
    fireEvent.click(screen.getByRole("button", { name: "Create" }));

    expect(mockToastError).toHaveBeenCalledWith("Pick a device for role spine");
    expect(mockInstantiate.mutateAsync).not.toHaveBeenCalled();
  });

  it("submits name and role_assignments once every role has a device, then navigates", async () => {
    renderPage();
    fireEvent.click(screen.getAllByText("Use")[0]);
    fireEvent.change(screen.getByLabelText("New topology name"), { target: { value: "Lab 1" } });
    const selects = screen.getAllByRole("combobox");
    fireEvent.change(selects[0], { target: { value: "dev-1" } });
    fireEvent.change(selects[1], { target: { value: "dev-2" } });
    fireEvent.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() =>
      expect(mockInstantiate.mutateAsync).toHaveBeenCalledWith({
        id: "tmpl-mine",
        name: "Lab 1",
        role_assignments: { spine: "dev-1", leaf: "dev-2" },
      }),
    );
    await waitFor(() => expect(mockNavigate).toHaveBeenCalledWith("/topology/new-topo"));
  });

  it("surfaces the backend detail on an instantiate failure", async () => {
    mockInstantiate.mutateAsync.mockRejectedValueOnce({
      response: { data: { detail: "role spine already committed" } },
    });
    renderPage();
    fireEvent.click(screen.getAllByText("Use")[0]);
    fireEvent.change(screen.getByLabelText("New topology name"), { target: { value: "Lab 1" } });
    const selects = screen.getAllByRole("combobox");
    fireEvent.change(selects[0], { target: { value: "dev-1" } });
    fireEvent.change(selects[1], { target: { value: "dev-2" } });
    fireEvent.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() =>
      expect(mockToastError).toHaveBeenCalledWith("role spine already committed"),
    );
    expect(mockNavigate).not.toHaveBeenCalled();
  });

  it("falls back to a generic instantiate error message with no response detail", async () => {
    mockInstantiate.mutateAsync.mockRejectedValueOnce(new Error("network down"));
    renderPage();
    fireEvent.click(screen.getAllByText("Use")[0]);
    fireEvent.change(screen.getByLabelText("New topology name"), { target: { value: "Lab 1" } });
    const selects = screen.getAllByRole("combobox");
    fireEvent.change(selects[0], { target: { value: "dev-1" } });
    fireEvent.change(selects[1], { target: { value: "dev-2" } });
    fireEvent.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() => expect(mockToastError).toHaveBeenCalledWith("Instantiate failed"));
  });

  it("Create is disabled while the name field is blank", () => {
    renderPage();
    fireEvent.click(screen.getAllByText("Use")[0]);
    expect(screen.getByRole("button", { name: "Create" })).toBeDisabled();
  });

  it("shows a loading message while the template detail is still loading", () => {
    mockUseTopologyTemplate.mockReturnValue({ data: undefined, isLoading: true });
    renderPage();
    fireEvent.click(screen.getAllByText("Use")[0]);
    expect(screen.getByText("Loading template...")).toBeInTheDocument();
  });
});
