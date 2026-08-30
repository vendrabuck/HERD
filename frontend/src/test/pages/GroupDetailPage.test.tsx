import { http, HttpResponse } from "msw";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type { ReactNode } from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";

// Toasts are fire-and-forget side effects; stub so save/assign paths do not
// blow up and so we can assert on the messages they emit.
const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }));
vi.mock("react-hot-toast", () => ({ default: toast }));

// useParams drives create-vs-edit mode; useNavigate is a side effect we want
// to observe.
let routeId: string | undefined;
const navigateSpy = vi.fn();
vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual<typeof import("react-router-dom")>("react-router-dom");
  return {
    ...actual,
    useParams: () => ({ id: routeId }),
    useNavigate: () => navigateSpy,
  };
});

// TransferList is exercised in its own component test; stub it so this page
// test can drive onAssign/onUnassign directly and assert data wiring
// (available/assigned counts) rather than the drag-between-lists UI.
const transferProps = vi.hoisted(() => ({ current: null as unknown }));
vi.mock("@/components/ui/TransferList", () => ({
  TransferList: (props: {
    availableItems: { id: string }[];
    assignedItems: { id: string }[];
    onAssign: (ids: string[]) => void;
    onUnassign: (ids: string[]) => void;
  }) => {
    transferProps.current = props;
    return (
      <div data-testid="transfer-list">
        available:{props.availableItems.length} assigned:{props.assignedItems.length}
        <button onClick={() => props.onAssign(["u-2"])}>assign-u2</button>
        <button onClick={() => props.onUnassign(["u-1"])}>unassign-u1</button>
      </div>
    );
  },
}));

import { server } from "../mocks/server";
import { GroupDetailPage } from "@/pages/admin/GroupDetailPage";
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
    name: "Network Team",
    description: "Owns switches",
    created_by: null,
    created_at: "2026-01-01T00:00:00Z",
    members: [
      { user_id: "u-1", username: "alice", email: "alice@b.c", added_at: "2026-01-02T00:00:00Z" },
    ],
    ...overrides,
  };
}

beforeEach(() => {
  routeId = GROUP_ID;
  navigateSpy.mockClear();
  toast.success.mockClear();
  toast.error.mockClear();
  transferProps.current = null;
  useAuthStore.setState({
    user: { id: "1", username: "admin", email: "a@b.c", role: "admin" },
  } as never);
  server.use(
    http.get("/api/auth/users", () =>
      HttpResponse.json({
        items: [
          { id: "u-1", username: "alice", email: "alice@b.c", role: "user" },
          { id: "u-2", username: "bob", email: "bob@b.c", role: "user" },
        ],
        total: 2,
        skip: 0,
        limit: 500,
      }),
    ),
  );
});

describe("GroupDetailPage", () => {
  it("renders the create form when there is no route id, with no members section", () => {
    routeId = undefined;
    renderWithProviders(<GroupDetailPage />);

    expect(screen.getByText("Create User Group")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create" })).toBeInTheDocument();
    expect(screen.queryByText(/^Members \(/)).not.toBeInTheDocument();
  });

  it("shows the loading state while an existing group is fetched", () => {
    server.use(
      http.get(`/api/auth/groups/${GROUP_ID}`, async () => {
        await new Promise(() => {});
        return HttpResponse.json({});
      }),
    );
    renderWithProviders(<GroupDetailPage />);
    expect(screen.getByText(/Loading group/i)).toBeInTheDocument();
  });

  it("hydrates the form fields and renders member count from the fetched group", async () => {
    server.use(
      http.get(`/api/auth/groups/${GROUP_ID}`, () => HttpResponse.json(makeGroupDetail())),
    );
    renderWithProviders(<GroupDetailPage />);

    await waitFor(() => expect(screen.getByLabelText("Name")).toHaveValue("Network Team"));
    expect(screen.getByLabelText("Description")).toHaveValue("Owns switches");
    expect(screen.getByText("Edit User Group")).toBeInTheDocument();
    expect(screen.getByText("Members (1)")).toBeInTheDocument();
  });

  it("excludes existing members from the available transfer list", async () => {
    server.use(
      http.get(`/api/auth/groups/${GROUP_ID}`, () => HttpResponse.json(makeGroupDetail())),
    );
    renderWithProviders(<GroupDetailPage />);

    await waitFor(() =>
      expect(screen.getByTestId("transfer-list")).toHaveTextContent("available:1 assigned:1"),
    );
  });

  it("blocks save and shows a validation toast when the name is empty", async () => {
    server.use(
      http.get(`/api/auth/groups/${GROUP_ID}`, () =>
        HttpResponse.json(makeGroupDetail({ name: "" })),
      ),
    );
    renderWithProviders(<GroupDetailPage />);
    await waitFor(() => expect(screen.getByText("Edit User Group")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(toast.error).toHaveBeenCalledWith("Group name is required"));
    expect(toast.success).not.toHaveBeenCalled();
  });

  it("creates a group and navigates to its detail route", async () => {
    routeId = undefined;
    let captured: unknown = null;
    server.use(
      http.post("/api/auth/groups", async ({ request }) => {
        captured = await request.json();
        return HttpResponse.json({ id: "new-id", name: "New Team", description: null });
      }),
    );
    renderWithProviders(<GroupDetailPage />);

    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "New Team" } });
    fireEvent.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() => expect(toast.success).toHaveBeenCalledWith("User group created"));
    expect(captured).toEqual({ name: "New Team", description: null });
    expect(navigateSpy).toHaveBeenCalledWith("/admin/groups/new-id", { replace: true });
  });

  it("saves an edit to an existing group without navigating", async () => {
    let captured: unknown = null;
    server.use(
      http.get(`/api/auth/groups/${GROUP_ID}`, () => HttpResponse.json(makeGroupDetail())),
      http.put(`/api/auth/groups/${GROUP_ID}`, async ({ request }) => {
        captured = await request.json();
        return HttpResponse.json(makeGroupDetail({ name: "Renamed Team" }));
      }),
    );
    renderWithProviders(<GroupDetailPage />);
    await waitFor(() => expect(screen.getByLabelText("Name")).toHaveValue("Network Team"));

    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Renamed Team" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(toast.success).toHaveBeenCalledWith("User group updated"));
    expect(captured).toEqual({ name: "Renamed Team", description: "Owns switches" });
    expect(navigateSpy).not.toHaveBeenCalled();
  });

  it("surfaces the server detail message when save fails", async () => {
    server.use(
      http.get(`/api/auth/groups/${GROUP_ID}`, () => HttpResponse.json(makeGroupDetail())),
      http.put(`/api/auth/groups/${GROUP_ID}`, () =>
        HttpResponse.json({ detail: "name already taken" }, { status: 409 }),
      ),
    );
    renderWithProviders(<GroupDetailPage />);
    await waitFor(() => expect(screen.getByLabelText("Name")).toHaveValue("Network Team"));

    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(toast.error).toHaveBeenCalledWith("name already taken"));
  });

  it("falls back to a generic message when save fails with no detail", async () => {
    server.use(
      http.get(`/api/auth/groups/${GROUP_ID}`, () => HttpResponse.json(makeGroupDetail())),
      http.put(`/api/auth/groups/${GROUP_ID}`, () => new HttpResponse(null, { status: 500 })),
    );
    renderWithProviders(<GroupDetailPage />);
    await waitFor(() => expect(screen.getByLabelText("Name")).toHaveValue("Network Team"));

    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(toast.error).toHaveBeenCalledWith("Failed to save group"));
  });

  it("assigns a member and shows the added-with-skipped count", async () => {
    server.use(
      http.get(`/api/auth/groups/${GROUP_ID}`, () => HttpResponse.json(makeGroupDetail())),
      http.post(`/api/auth/groups/${GROUP_ID}/members/bulk`, () =>
        HttpResponse.json({ added: 1, skipped: 1 }),
      ),
    );
    renderWithProviders(<GroupDetailPage />);
    await screen.findByTestId("transfer-list");

    fireEvent.click(screen.getByRole("button", { name: "assign-u2" }));

    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith("1 member(s) added, 1 skipped"),
    );
  });

  it("assigns a member and omits the skipped clause when nothing was skipped", async () => {
    server.use(
      http.get(`/api/auth/groups/${GROUP_ID}`, () => HttpResponse.json(makeGroupDetail())),
      http.post(`/api/auth/groups/${GROUP_ID}/members/bulk`, () =>
        HttpResponse.json({ added: 1, skipped: 0 }),
      ),
    );
    renderWithProviders(<GroupDetailPage />);
    await screen.findByTestId("transfer-list");

    fireEvent.click(screen.getByRole("button", { name: "assign-u2" }));

    await waitFor(() => expect(toast.success).toHaveBeenCalledWith("1 member(s) added"));
  });

  it("surfaces the server detail message when assigning a member fails", async () => {
    server.use(
      http.get(`/api/auth/groups/${GROUP_ID}`, () => HttpResponse.json(makeGroupDetail())),
      http.post(`/api/auth/groups/${GROUP_ID}/members/bulk`, () =>
        HttpResponse.json({ detail: "user not found" }, { status: 404 }),
      ),
    );
    renderWithProviders(<GroupDetailPage />);
    await screen.findByTestId("transfer-list");

    fireEvent.click(screen.getByRole("button", { name: "assign-u2" }));

    await waitFor(() => expect(toast.error).toHaveBeenCalledWith("user not found"));
  });

  it("falls back to a generic message when assigning a member fails with no detail", async () => {
    server.use(
      http.get(`/api/auth/groups/${GROUP_ID}`, () => HttpResponse.json(makeGroupDetail())),
      http.post(`/api/auth/groups/${GROUP_ID}/members/bulk`, () =>
        new HttpResponse(null, { status: 500 }),
      ),
    );
    renderWithProviders(<GroupDetailPage />);
    await screen.findByTestId("transfer-list");

    fireEvent.click(screen.getByRole("button", { name: "assign-u2" }));

    await waitFor(() => expect(toast.error).toHaveBeenCalledWith("Failed to add members"));
  });

  it("unassigns a member and shows the removed count", async () => {
    server.use(
      http.get(`/api/auth/groups/${GROUP_ID}`, () => HttpResponse.json(makeGroupDetail())),
      http.post(`/api/auth/groups/${GROUP_ID}/members/bulk-remove`, () =>
        HttpResponse.json({ removed: 1, not_found: 0 }),
      ),
    );
    renderWithProviders(<GroupDetailPage />);
    await screen.findByTestId("transfer-list");

    fireEvent.click(screen.getByRole("button", { name: "unassign-u1" }));

    await waitFor(() => expect(toast.success).toHaveBeenCalledWith("1 member(s) removed"));
  });

  it("surfaces the server detail message when unassigning a member fails", async () => {
    server.use(
      http.get(`/api/auth/groups/${GROUP_ID}`, () => HttpResponse.json(makeGroupDetail())),
      http.post(`/api/auth/groups/${GROUP_ID}/members/bulk-remove`, () =>
        HttpResponse.json({ detail: "cannot remove last member" }, { status: 409 }),
      ),
    );
    renderWithProviders(<GroupDetailPage />);
    await screen.findByTestId("transfer-list");

    fireEvent.click(screen.getByRole("button", { name: "unassign-u1" }));

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith("cannot remove last member"),
    );
  });

  it("falls back to a generic message when unassigning a member fails with no detail", async () => {
    server.use(
      http.get(`/api/auth/groups/${GROUP_ID}`, () => HttpResponse.json(makeGroupDetail())),
      http.post(`/api/auth/groups/${GROUP_ID}/members/bulk-remove`, () =>
        new HttpResponse(null, { status: 500 }),
      ),
    );
    renderWithProviders(<GroupDetailPage />);
    await screen.findByTestId("transfer-list");

    fireEvent.click(screen.getByRole("button", { name: "unassign-u1" }));

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith("Failed to remove members"),
    );
  });

  it("does not fetch the user list for a non-admin viewer", async () => {
    useAuthStore.setState({
      user: { id: "2", username: "viewer", email: "v@b.c", role: "user" },
    } as never);
    let usersRequested = false;
    server.use(
      http.get(`/api/auth/groups/${GROUP_ID}`, () => HttpResponse.json(makeGroupDetail())),
      http.get("/api/auth/users", () => {
        usersRequested = true;
        return HttpResponse.json({ items: [], total: 0, skip: 0, limit: 500 });
      }),
    );
    renderWithProviders(<GroupDetailPage />);
    await screen.findByTestId("transfer-list");

    expect(usersRequested).toBe(false);
    // With no user list, the only "available" candidate would come from
    // allUsers, so the available column stays empty even though members exist.
    expect(screen.getByTestId("transfer-list")).toHaveTextContent("available:0 assigned:1");
  });
});
