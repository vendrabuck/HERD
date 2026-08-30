import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, vi, beforeEach } from "vitest";

// Mock react-router-dom
const mockNavigate = vi.fn();
vi.mock("react-router-dom", () => ({
  useNavigate: () => mockNavigate,
}));

// Mock the bulk import/export API calls so BulkImportExport renders inert.
vi.mock("@/api/bulk", () => ({
  exportTopologies: vi.fn(),
  importTopologies: vi.fn(),
}));

// Mock authStore
let mockUser: { id: string; role: string } | null = {
  id: "user-1",
  role: "user",
};
vi.mock("@/stores/authStore", () => ({
  useAuthStore: (selector: (s: { user: typeof mockUser }) => unknown) =>
    selector({ user: mockUser }),
}));

// Mock topology hooks
const mockUsePaginatedTopologies = vi.fn();
const mockCreateTopology = { mutateAsync: vi.fn(), isPending: false };
const mockDeleteTopology = { mutateAsync: vi.fn(), isPending: false };
const mockCloneTopology = { mutateAsync: vi.fn(), isPending: false };

vi.mock("@/api/topologies", () => ({
  usePaginatedTopologies: (...args: unknown[]) => mockUsePaginatedTopologies(...args),
  useCreateTopology: () => mockCreateTopology,
  useDeleteTopology: () => mockDeleteTopology,
  useCloneTopology: () => mockCloneTopology,
}));

import { TopologyPage } from "@/pages/TopologyPage";
import type { Topology } from "@/types/topology.types";

const TOPO_MINE: Topology = {
  id: "topo-mine",
  name: "My Lab",
  description: null,
  topology_type: "PHYSICAL",
  canvas_data: null,
  version_number: 1,
  created_by: "user-1",
  owner_name: "Me",
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-02T00:00:00Z",
} as unknown as Topology;

const TOPO_OTHER: Topology = {
  id: "topo-other",
  name: "Their Lab",
  description: null,
  topology_type: "PHYSICAL",
  canvas_data: null,
  version_number: 1,
  created_by: "user-2",
  owner_name: "Someone Else",
  created_at: "2026-01-03T00:00:00Z",
  updated_at: "2026-01-04T00:00:00Z",
} as unknown as Topology;

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <TopologyPage />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  mockUser = { id: "user-1", role: "user" };
  mockCreateTopology.mutateAsync.mockResolvedValue({ id: "new-topo" });
  mockDeleteTopology.mutateAsync.mockResolvedValue(undefined);
  mockCloneTopology.mutateAsync.mockResolvedValue({ id: "cloned-topo" });
  mockUsePaginatedTopologies.mockReturnValue({
    data: { items: [TOPO_MINE, TOPO_OTHER], total: 2, skip: 0, limit: 50 },
    isLoading: false,
    isError: false,
  });
});

describe("TopologyPage loading/error/empty states", () => {
  it("shows the loading status text while fetching", () => {
    mockUsePaginatedTopologies.mockReturnValue({ data: undefined, isLoading: true, isError: false });
    renderPage();
    expect(screen.getByRole("status")).toHaveTextContent("Loading topologies...");
  });

  it("shows the exact error text on a failed fetch", () => {
    mockUsePaginatedTopologies.mockReturnValue({ data: undefined, isLoading: false, isError: true });
    renderPage();
    expect(screen.getByText("Failed to load topologies")).toBeInTheDocument();
  });

  it("shows the exact empty-state text when there are zero topologies", () => {
    mockUsePaginatedTopologies.mockReturnValue({
      data: { items: [], total: 0, skip: 0, limit: 50 },
      isLoading: false,
      isError: false,
    });
    renderPage();
    expect(
      screen.getByText("No topologies yet. Create one to get started."),
    ).toBeInTheDocument();
  });
});

describe("TopologyPage list rendering and row permissions", () => {
  it("renders rows with name, owner, and the topology count", () => {
    renderPage();
    expect(screen.getByText("Topologies")).toBeInTheDocument();
    expect(screen.getByText("(2)")).toBeInTheDocument();
    expect(screen.getByText("My Lab")).toBeInTheDocument();
    expect(screen.getByText("Their Lab")).toBeInTheDocument();
    expect(screen.getByText("Me")).toBeInTheDocument();
  });

  it("a non-admin sees Delete only on their own topology row", () => {
    renderPage();
    const rows = screen.getAllByRole("row").slice(1); // drop header row
    const mineRow = rows.find((r) => r.textContent?.includes("My Lab"))!;
    const otherRow = rows.find((r) => r.textContent?.includes("Their Lab"))!;
    expect(within(mineRow).getByText("Delete")).toBeInTheDocument();
    expect(within(otherRow).queryByText("Delete")).not.toBeInTheDocument();
  });

  it("an admin sees Delete on every row regardless of owner", () => {
    mockUser = { id: "user-1", role: "admin" };
    renderPage();
    const rows = screen.getAllByRole("row").slice(1);
    const otherRow = rows.find((r) => r.textContent?.includes("Their Lab"))!;
    expect(within(otherRow).getByText("Delete")).toBeInTheDocument();
  });

  it("clicking a row navigates to the topology editor by id", () => {
    renderPage();
    fireEvent.click(screen.getByText("My Lab"));
    expect(mockNavigate).toHaveBeenCalledWith("/topology/topo-mine");
  });
});

describe("TopologyPage create flow", () => {
  it("submits the trimmed name and navigates to the new topology on success", async () => {
    renderPage();
    fireEvent.click(screen.getByRole("button", { name: "New Topology" }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "  Fresh Lab  " } });
    fireEvent.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() =>
      expect(mockCreateTopology.mutateAsync).toHaveBeenCalledWith({ name: "Fresh Lab" }),
    );
    await waitFor(() => expect(mockNavigate).toHaveBeenCalledWith("/topology/new-topo"));
  });

  it("Create is disabled while the name field is blank", () => {
    renderPage();
    fireEvent.click(screen.getByRole("button", { name: "New Topology" }));
    expect(screen.getByRole("button", { name: "Create" })).toBeDisabled();
  });

  it("keeps the modal open and does not navigate when create fails", async () => {
    mockCreateTopology.mutateAsync.mockRejectedValueOnce(new Error("boom"));
    renderPage();
    fireEvent.click(screen.getByRole("button", { name: "New Topology" }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Fresh Lab" } });
    fireEvent.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() => expect(mockCreateTopology.mutateAsync).toHaveBeenCalledTimes(1));
    expect(mockNavigate).not.toHaveBeenCalled();
    // The name field survives so the user can retry without retyping.
    expect(screen.getByLabelText("Name")).toHaveValue("Fresh Lab");
  });
});

describe("TopologyPage clone flow", () => {
  it("pre-fills the clone name as '<name> (copy)' and submits it", async () => {
    renderPage();
    const rows = screen.getAllByRole("row").slice(1);
    const mineRow = rows.find((r) => r.textContent?.includes("My Lab"))!;
    fireEvent.click(within(mineRow).getByText("Clone"));

    expect(screen.getByLabelText("Name for the clone")).toHaveValue("My Lab (copy)");
    const dialog = screen.getByText("Clone Topology").closest("dialog")!;
    fireEvent.click(within(dialog).getByRole("button", { name: "Clone" }));

    await waitFor(() =>
      expect(mockCloneTopology.mutateAsync).toHaveBeenCalledWith({
        id: "topo-mine",
        name: "My Lab (copy)",
      }),
    );
    await waitFor(() => expect(mockNavigate).toHaveBeenCalledWith("/topology/cloned-topo"));
  });

  it("clicking Clone does not also trigger the row's navigate-to-editor handler", () => {
    renderPage();
    const rows = screen.getAllByRole("row").slice(1);
    const mineRow = rows.find((r) => r.textContent?.includes("My Lab"))!;
    fireEvent.click(within(mineRow).getByText("Clone"));
    expect(mockNavigate).not.toHaveBeenCalled();
  });
});

describe("TopologyPage delete flow", () => {
  it("confirming the delete dialog calls delete with the row id", async () => {
    renderPage();
    const rows = screen.getAllByRole("row").slice(1);
    const mineRow = rows.find((r) => r.textContent?.includes("My Lab"))!;
    fireEvent.click(within(mineRow).getByText("Delete"));

    const dialog = screen.getByText("Delete topology?").closest("dialog")!;
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete" }));

    await waitFor(() => expect(mockDeleteTopology.mutateAsync).toHaveBeenCalledWith("topo-mine"));
  });

  it("cancelling the delete dialog does not call delete", () => {
    renderPage();
    const rows = screen.getAllByRole("row").slice(1);
    const mineRow = rows.find((r) => r.textContent?.includes("My Lab"))!;
    fireEvent.click(within(mineRow).getByText("Delete"));
    const dialog = screen.getByText("Delete topology?").closest("dialog")!;
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(mockDeleteTopology.mutateAsync).not.toHaveBeenCalled();
  });

  it("clicking the row Delete button does not also navigate to the editor", () => {
    renderPage();
    const rows = screen.getAllByRole("row").slice(1);
    const mineRow = rows.find((r) => r.textContent?.includes("My Lab"))!;
    fireEvent.click(within(mineRow).getByText("Delete"));
    expect(mockNavigate).not.toHaveBeenCalled();
  });

  it("keeps the confirm dialog open (deleteId retained) when the delete mutation fails", async () => {
    mockDeleteTopology.mutateAsync.mockRejectedValueOnce(new Error("boom"));
    renderPage();
    const rows = screen.getAllByRole("row").slice(1);
    const mineRow = rows.find((r) => r.textContent?.includes("My Lab"))!;
    fireEvent.click(within(mineRow).getByText("Delete"));
    const dialog = screen.getByText("Delete topology?").closest("dialog")!;
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete" }));

    await waitFor(() => expect(mockDeleteTopology.mutateAsync).toHaveBeenCalledTimes(1));
    // The dialog is still open for a retry, unlike a successful delete which closes it.
    expect(screen.getByText("Delete topology?")).toBeInTheDocument();
  });
});

describe("TopologyPage modal Cancel buttons", () => {
  it("New Topology modal Cancel closes the dialog (open attribute cleared) without creating", () => {
    renderPage();
    fireEvent.click(screen.getByRole("button", { name: "New Topology" }));
    const dialog = screen.getByLabelText("Name").closest("dialog")!;
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Draft" } });
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(dialog).not.toHaveAttribute("open");
    expect(mockCreateTopology.mutateAsync).not.toHaveBeenCalled();
  });

  it("Clone Topology modal Cancel closes the dialog without submitting", () => {
    renderPage();
    const rows = screen.getAllByRole("row").slice(1);
    const mineRow = rows.find((r) => r.textContent?.includes("My Lab"))!;
    fireEvent.click(within(mineRow).getByText("Clone"));
    const dialog = screen.getByText("Clone Topology").closest("dialog")!;
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(dialog).not.toHaveAttribute("open");
    expect(mockCloneTopology.mutateAsync).not.toHaveBeenCalled();
  });

  it("keeps the clone modal open and does not navigate when clone fails", async () => {
    mockCloneTopology.mutateAsync.mockRejectedValueOnce(new Error("boom"));
    renderPage();
    const rows = screen.getAllByRole("row").slice(1);
    const mineRow = rows.find((r) => r.textContent?.includes("My Lab"))!;
    fireEvent.click(within(mineRow).getByText("Clone"));
    const dialog = screen.getByText("Clone Topology").closest("dialog")!;
    fireEvent.click(within(dialog).getByRole("button", { name: "Clone" }));

    await waitFor(() => expect(mockCloneTopology.mutateAsync).toHaveBeenCalledTimes(1));
    expect(mockNavigate).not.toHaveBeenCalled();
    expect(screen.getByLabelText("Name for the clone")).toHaveValue("My Lab (copy)");
  });
});
