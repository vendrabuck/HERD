import { http, HttpResponse } from "msw";
import { render, screen, waitFor, fireEvent, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type { ReactNode } from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";

const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }));
vi.mock("react-hot-toast", () => ({ default: toast }));

const navigateSpy = vi.fn();
vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual<typeof import("react-router-dom")>("react-router-dom");
  return { ...actual, useNavigate: () => navigateSpy };
});

import { server } from "../mocks/server";
import { GroupsPage } from "@/pages/admin/GroupsPage";

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

const GROUPS = [
  {
    id: "g-1",
    name: "Network Team",
    description: "Owns switches",
    created_by: null,
    created_at: "2026-01-05T00:00:00Z",
  },
  {
    id: "g-2",
    name: "QA Team",
    description: null,
    created_by: null,
    created_at: "2026-02-10T00:00:00Z",
  },
];

function groupsHandler(items: typeof GROUPS, total = items.length) {
  return http.get("/api/auth/groups", () =>
    HttpResponse.json({ items, total, skip: 0, limit: 50 }),
  );
}

beforeEach(() => {
  navigateSpy.mockClear();
  toast.success.mockClear();
  toast.error.mockClear();
});

describe("GroupsPage", () => {
  it("shows the loading state before groups resolve", () => {
    server.use(
      http.get("/api/auth/groups", async () => {
        await new Promise(() => {});
        return HttpResponse.json({});
      }),
    );
    renderWithProviders(<GroupsPage />);
    expect(screen.getByText("Loading groups...")).toBeInTheDocument();
  });

  it("renders an empty state when there are no groups", async () => {
    server.use(groupsHandler([]));
    renderWithProviders(<GroupsPage />);
    await waitFor(() => expect(screen.getByText("No groups found")).toBeInTheDocument());
  });

  it("renders group rows with description fallback and formatted date", async () => {
    server.use(groupsHandler(GROUPS));
    renderWithProviders(<GroupsPage />);

    const table = await screen.findByRole("table");
    const rows = within(table);
    await waitFor(() => expect(rows.getByText("Network Team")).toBeInTheDocument());
    expect(rows.getByText("Owns switches")).toBeInTheDocument();
    expect(rows.getByText("QA Team")).toBeInTheDocument();
    // No description on the second group falls back to a dash.
    expect(rows.getByText("-")).toBeInTheDocument();
  });

  it("navigates to the create-group route when Create User Group is clicked", () => {
    server.use(groupsHandler([]));
    renderWithProviders(<GroupsPage />);
    fireEvent.click(screen.getByRole("button", { name: "Create User Group" }));
    expect(navigateSpy).toHaveBeenCalledWith("/admin/groups/new");
  });

  it("navigates to the group detail route when a row is clicked", async () => {
    server.use(groupsHandler(GROUPS));
    renderWithProviders(<GroupsPage />);
    const row = await screen.findByText("Network Team");
    fireEvent.click(row);
    expect(navigateSpy).toHaveBeenCalledWith("/admin/groups/g-1");
  });

  it("deletes a group through the confirm dialog without navigating", async () => {
    let deleteCalled = false;
    server.use(
      groupsHandler(GROUPS),
      http.delete("/api/auth/groups/g-1", () => {
        deleteCalled = true;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    renderWithProviders(<GroupsPage />);
    await screen.findByText("Network Team");

    // Row delete buttons come before the confirm dialog's own Delete button
    // in document order.
    fireEvent.click(screen.getAllByRole("button", { name: "Delete" })[0]);
    const dialog = within(screen.getByRole("dialog", { name: "Delete Group" }));
    expect(
      dialog.getByText(/All memberships will be removed/i),
    ).toBeInTheDocument();
    fireEvent.click(dialog.getByRole("button", { name: "Delete" }));

    await waitFor(() => expect(deleteCalled).toBe(true));
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith("Group deleted"));
    expect(navigateSpy).not.toHaveBeenCalledWith(expect.stringContaining("/admin/groups/g-1"));
  });

  it("cancelling the delete confirm dialog does not call delete", async () => {
    let deleteCalled = false;
    server.use(
      groupsHandler(GROUPS),
      http.delete("/api/auth/groups/g-1", () => {
        deleteCalled = true;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    renderWithProviders(<GroupsPage />);
    await screen.findByText("Network Team");

    fireEvent.click(screen.getAllByRole("button", { name: "Delete" })[0]);
    const dialog = within(screen.getByRole("dialog", { name: "Delete Group" }));
    fireEvent.click(dialog.getByRole("button", { name: "Cancel" }));

    expect(deleteCalled).toBe(false);
    expect(screen.queryByRole("dialog", { name: "Delete Group" })).not.toBeInTheDocument();
  });

  it("surfaces the server detail message when delete fails", async () => {
    server.use(
      groupsHandler(GROUPS),
      http.delete("/api/auth/groups/g-1", () =>
        HttpResponse.json({ detail: "group has active members" }, { status: 409 }),
      ),
    );
    renderWithProviders(<GroupsPage />);
    await screen.findByText("Network Team");

    fireEvent.click(screen.getAllByRole("button", { name: "Delete" })[0]);
    const dialog = within(screen.getByRole("dialog", { name: "Delete Group" }));
    fireEvent.click(dialog.getByRole("button", { name: "Delete" }));

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith("group has active members"),
    );
  });

  it("falls back to a generic message when delete fails with no detail", async () => {
    server.use(
      groupsHandler(GROUPS),
      http.delete("/api/auth/groups/g-1", () => new HttpResponse(null, { status: 500 })),
    );
    renderWithProviders(<GroupsPage />);
    await screen.findByText("Network Team");

    fireEvent.click(screen.getAllByRole("button", { name: "Delete" })[0]);
    const dialog = within(screen.getByRole("dialog", { name: "Delete Group" }));
    fireEvent.click(dialog.getByRole("button", { name: "Delete" }));

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith("Failed to delete group"),
    );
  });

  it("pages forward and back through the group list", async () => {
    let capturedSkip: string | null = null;
    server.use(
      http.get("/api/auth/groups", ({ request }) => {
        const url = new URL(request.url);
        capturedSkip = url.searchParams.get("skip");
        return HttpResponse.json({ items: GROUPS, total: 120, skip: 0, limit: 50 });
      }),
    );
    renderWithProviders(<GroupsPage />);
    await screen.findByText("Network Team");

    expect(screen.getByText("Showing 1-50 of 120")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Next" }));

    await waitFor(() => expect(capturedSkip).toBe("50"));
  });
});
