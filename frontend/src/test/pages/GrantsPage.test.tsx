import { http, HttpResponse } from "msw";
import { render, screen, waitFor, fireEvent, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type { ReactNode } from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";

// setup.ts already polyfills HTMLDialogElement.showModal/close to toggle the
// `open` attribute, so dialogs become queryable by the "dialog" role. Do not
// override it with no-op spies here, or the dialog never opens.

// Spy on the toast surface so we can assert success and validation feedback.
const toastSuccess = vi.fn();
const toastError = vi.fn();
vi.mock("react-hot-toast", () => ({
  default: { success: (m: string) => toastSuccess(m), error: (m: string) => toastError(m) },
}));

// The page is reached only through the AdminGuard route group in routes.tsx,
// so admin-ness is
// not this page's concern; pin an admin user anyway since apiClient's
// request interceptor reads useAuthStore.getState().accessToken on every
// call, so the mock must expose getState as well as the selector hook, or
// every axios request throws and no network calls ever reach MSW.
const AUTH_STATE = {
  user: { id: "1", role: "admin", username: "admin", email: "a@b.c" },
  accessToken: "t",
  refreshToken: null,
  clearAuth: () => {},
  setTokens: () => {},
};
vi.mock("@/stores/authStore", () => {
  const useAuthStore = (sel: (s: unknown) => unknown) => sel(AUTH_STATE);
  useAuthStore.getState = () => AUTH_STATE;
  useAuthStore.setState = () => {};
  return { useAuthStore };
});

import { server } from "../mocks/server";
import { GrantsPage } from "@/pages/admin/GrantsPage";

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
  { id: "grp-1", name: "Network Team", description: null, created_by: null, created_at: "2026-01-01T00:00:00Z" },
  { id: "grp-2", name: "QA Team", description: null, created_by: null, created_at: "2026-01-01T00:00:00Z" },
];

const GRANT = {
  id: "grant-1",
  group_id: "grp-1",
  resource_type: "device",
  resource_id: "11111111-1111-1111-1111-111111111111",
  permission: "view",
  granted_by: "admin-1",
  granted_at: "2026-07-01T00:00:00Z",
};

function grantsHandler(items: typeof GRANT[]) {
  return http.get("/api/acl/grants", () =>
    HttpResponse.json({ items, total: items.length, skip: 0, limit: 50 }),
  );
}

beforeEach(() => {
  toastSuccess.mockClear();
  toastError.mockClear();
  server.use(
    http.get("/api/auth/groups", () =>
      HttpResponse.json({ items: GROUPS, total: GROUPS.length, skip: 0, limit: 500 }),
    ),
  );
});

// The row action and the confirm dialog both label a button "Delete". The
// table renders before the ConfirmDialog in JSX, so the row button is first
// in document order; the confirm button is then scoped via the dialog role.
function clickRowDelete() {
  return screen.getAllByRole("button", { name: "Delete" })[0];
}

describe("GrantsPage", () => {
  it("shows the loading state before grants resolve", () => {
    server.use(
      http.get("/api/acl/grants", async () => {
        await new Promise(() => {});
        return HttpResponse.json({});
      }),
    );
    renderWithProviders(<GrantsPage />);
    expect(screen.getByText(/Loading grants/i)).toBeInTheDocument();
  });

  it("renders an empty state when there are no grants", async () => {
    server.use(grantsHandler([]));
    renderWithProviders(<GrantsPage />);
    await waitFor(() => expect(screen.getByText("No grants found")).toBeInTheDocument());
  });

  it("renders a grant row resolved to its group name", async () => {
    server.use(grantsHandler([GRANT]));
    renderWithProviders(<GrantsPage />);

    // The filter select and the create modal's selects also render "device",
    // "view", etc. as <option> text (the modal is mounted but closed, not
    // unmounted), so scope assertions to the grants table itself.
    const table = await screen.findByRole("table");
    const row = within(table);
    await waitFor(() => expect(row.getByText("Network Team")).toBeInTheDocument());
    expect(row.getByText("device")).toBeInTheDocument();
    expect(row.getByText("view")).toBeInTheDocument();
    expect(row.getByText(GRANT.resource_id)).toBeInTheDocument();
  });

  it("blocks create and shows a validation toast when no group is selected", async () => {
    server.use(grantsHandler([]));
    renderWithProviders(<GrantsPage />);
    await waitFor(() => expect(screen.getByText("No grants found")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "Create Grant" }));
    fireEvent.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() => expect(toastError).toHaveBeenCalledWith("Group is required"));
    expect(toastSuccess).not.toHaveBeenCalled();
  });

  it("blocks create and shows a validation toast when resource id is not a uuid", async () => {
    server.use(grantsHandler([]));
    renderWithProviders(<GrantsPage />);
    await waitFor(() => expect(screen.getByText("No grants found")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "Create Grant" }));
    const dialog = screen.getByRole("dialog", { name: "Create Grant" });
    const form = within(dialog);
    fireEvent.change(form.getByLabelText("Group"), { target: { value: "grp-1" } });
    fireEvent.change(form.getByLabelText("Resource ID"), { target: { value: "not-a-uuid" } });
    fireEvent.click(form.getByRole("button", { name: "Create" }));

    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith("Resource ID must be a valid UUID"),
    );
    expect(toastSuccess).not.toHaveBeenCalled();
  });

  it("creates a grant through the modal and posts the right payload", async () => {
    let captured: unknown = null;
    server.use(
      grantsHandler([]),
      http.post("/api/acl/grants", async ({ request }) => {
        captured = await request.json();
        return HttpResponse.json(GRANT, { status: 201 });
      }),
    );
    renderWithProviders(<GrantsPage />);
    await waitFor(() => expect(screen.getByText("No grants found")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "Create Grant" }));
    const dialog = screen.getByRole("dialog", { name: "Create Grant" });
    const form = within(dialog);

    fireEvent.change(form.getByLabelText("Group"), { target: { value: "grp-1" } });
    fireEvent.change(form.getByLabelText("Resource Type"), { target: { value: "topology" } });
    fireEvent.change(form.getByLabelText("Resource ID"), {
      target: { value: "22222222-2222-2222-2222-222222222222" },
    });
    fireEvent.change(form.getByLabelText("Permission"), { target: { value: "manage" } });
    fireEvent.click(form.getByRole("button", { name: "Create" }));

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith("Grant created"));
    expect(captured).toEqual({
      group_id: "grp-1",
      resource_type: "topology",
      resource_id: "22222222-2222-2222-2222-222222222222",
      permission: "manage",
    });
  });

  it("surfaces the server detail message when create fails", async () => {
    server.use(
      grantsHandler([]),
      http.post("/api/acl/grants", () =>
        HttpResponse.json({ detail: "This grant already exists" }, { status: 409 }),
      ),
    );
    renderWithProviders(<GrantsPage />);
    await waitFor(() => expect(screen.getByText("No grants found")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "Create Grant" }));
    const dialog = screen.getByRole("dialog", { name: "Create Grant" });
    const form = within(dialog);
    fireEvent.change(form.getByLabelText("Group"), { target: { value: "grp-1" } });
    fireEvent.change(form.getByLabelText("Resource ID"), {
      target: { value: "22222222-2222-2222-2222-222222222222" },
    });
    fireEvent.click(form.getByRole("button", { name: "Create" }));

    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith("This grant already exists"),
    );
  });

  it("falls back to a generic message instead of rendering a list-shaped detail", async () => {
    // FastAPI/pydantic validation errors shape `detail` as a list of error
    // objects, not a string. Passing that straight to toast.error would throw
    // "Objects are not valid as a React child" inside the Toaster, which
    // sits outside the app's ErrorBoundary and would blank the whole page.
    server.use(
      grantsHandler([]),
      http.post("/api/acl/grants", () =>
        HttpResponse.json(
          { detail: [{ type: "uuid_parsing", loc: ["body", "resource_id"], msg: "bad uuid" }] },
          { status: 422 },
        ),
      ),
    );
    renderWithProviders(<GrantsPage />);
    await waitFor(() => expect(screen.getByText("No grants found")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "Create Grant" }));
    const dialog = screen.getByRole("dialog", { name: "Create Grant" });
    const form = within(dialog);
    fireEvent.change(form.getByLabelText("Group"), { target: { value: "grp-1" } });
    // A syntactically valid UUID so the client-side check passes and the
    // request actually reaches the (mocked) 422 server response.
    fireEvent.change(form.getByLabelText("Resource ID"), {
      target: { value: "22222222-2222-2222-2222-222222222222" },
    });
    fireEvent.click(form.getByRole("button", { name: "Create" }));

    await waitFor(() => expect(toastError).toHaveBeenCalledWith("Failed to create grant"));
  });

  it("renders an error state when the grants query fails", async () => {
    server.use(
      http.get("/api/acl/grants", () => HttpResponse.json({ detail: "boom" }, { status: 500 })),
    );
    renderWithProviders(<GrantsPage />);
    await waitFor(() => expect(screen.getByText("Failed to load grants")).toBeInTheDocument());
    expect(screen.queryByText("No grants found")).not.toBeInTheDocument();
  });

  it("deletes a grant through the confirm dialog", async () => {
    let deleteCalled = false;
    server.use(
      grantsHandler([GRANT]),
      http.delete("/api/acl/grants/grant-1", () => {
        deleteCalled = true;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    renderWithProviders(<GrantsPage />);
    const table = await screen.findByRole("table");
    await within(table).findByText("Network Team");

    fireEvent.click(clickRowDelete());
    const confirm = within(screen.getByRole("dialog", { name: /Delete Grant/i }));
    fireEvent.click(confirm.getByRole("button", { name: "Delete" }));

    await waitFor(() => expect(deleteCalled).toBe(true));
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith("Grant deleted"));
  });

  it("surfaces the server detail message when delete fails", async () => {
    server.use(
      grantsHandler([GRANT]),
      http.delete("/api/acl/grants/grant-1", () =>
        HttpResponse.json({ detail: "grant not found" }, { status: 404 }),
      ),
    );
    renderWithProviders(<GrantsPage />);
    const table = await screen.findByRole("table");
    await within(table).findByText("Network Team");

    fireEvent.click(clickRowDelete());
    const confirm = within(screen.getByRole("dialog", { name: /Delete Grant/i }));
    fireEvent.click(confirm.getByRole("button", { name: "Delete" }));

    await waitFor(() => expect(toastError).toHaveBeenCalledWith("grant not found"));
  });

  it("blocks create with a validation toast when the resource id is left blank", async () => {
    server.use(grantsHandler([]));
    renderWithProviders(<GrantsPage />);
    await waitFor(() => expect(screen.getByText("No grants found")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "Create Grant" }));
    const dialog = screen.getByRole("dialog", { name: "Create Grant" });
    const form = within(dialog);
    fireEvent.change(form.getByLabelText("Group"), { target: { value: "grp-1" } });
    fireEvent.click(form.getByRole("button", { name: "Create" }));

    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith("Resource ID is required"),
    );
    expect(toastSuccess).not.toHaveBeenCalled();
  });

  it("cancel closes the create modal without submitting", async () => {
    server.use(grantsHandler([]));
    renderWithProviders(<GrantsPage />);
    await waitFor(() => expect(screen.getByText("No grants found")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "Create Grant" }));
    const dialog = screen.getByRole("dialog", { name: "Create Grant" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));

    expect(screen.queryByRole("dialog", { name: "Create Grant" })).not.toBeInTheDocument();
    expect(toastSuccess).not.toHaveBeenCalled();
    expect(toastError).not.toHaveBeenCalled();
  });

  it("cancelling the delete confirm dialog issues no delete call", async () => {
    let deleteCalled = false;
    server.use(
      grantsHandler([GRANT]),
      http.delete("/api/acl/grants/grant-1", () => {
        deleteCalled = true;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    renderWithProviders(<GrantsPage />);
    const table = await screen.findByRole("table");
    await within(table).findByText("Network Team");

    fireEvent.click(clickRowDelete());
    const confirm = within(screen.getByRole("dialog", { name: /Delete Grant/i }));
    fireEvent.click(confirm.getByRole("button", { name: "Cancel" }));

    expect(deleteCalled).toBe(false);
  });

  describe("filters", () => {
    it("filters by group, then resource type, and resets to the first page on each change", async () => {
      const requests: { group_id: string | null; resource_type: string | null; skip: string | null }[] = [];
      server.use(
        http.get("/api/acl/grants", ({ request }) => {
          const url = new URL(request.url);
          requests.push({
            group_id: url.searchParams.get("group_id"),
            resource_type: url.searchParams.get("resource_type"),
            skip: url.searchParams.get("skip"),
          });
          return HttpResponse.json({ items: [GRANT], total: 1, skip: 0, limit: 50 });
        }),
      );
      renderWithProviders(<GrantsPage />);
      await waitFor(() => expect(requests.length).toBeGreaterThan(0));

      // The create modal (closed, but always mounted) has its own "Group"
      // and "Resource Type" labels, so getByLabelText would find two matches;
      // the filter controls have distinct ids to select by instead.
      const filterGroup = document.getElementById("grant-filter-group") as HTMLSelectElement;
      const filterResourceType = document.getElementById(
        "grant-filter-resource-type",
      ) as HTMLSelectElement;

      fireEvent.change(filterGroup, { target: { value: "grp-2" } });
      await waitFor(() =>
        expect(requests[requests.length - 1]).toEqual({
          group_id: "grp-2",
          resource_type: null,
          skip: "0",
        }),
      );

      fireEvent.change(filterResourceType, { target: { value: "topology" } });
      await waitFor(() =>
        expect(requests[requests.length - 1]).toEqual({
          group_id: "grp-2",
          resource_type: "topology",
          skip: "0",
        }),
      );
    });

    it("ignores a partial resource-id filter as unparseable, then applies it once it is a full uuid", async () => {
      const requests: (string | null)[] = [];
      server.use(
        http.get("/api/acl/grants", ({ request }) => {
          requests.push(new URL(request.url).searchParams.get("resource_id"));
          return HttpResponse.json({ items: [GRANT], total: 1, skip: 0, limit: 50 });
        }),
      );
      renderWithProviders(<GrantsPage />);
      await waitFor(() => expect(requests.length).toBeGreaterThan(0));

      const filterResourceId = document.getElementById(
        "grant-filter-resource-id",
      ) as HTMLInputElement;

      fireEvent.change(filterResourceId, { target: { value: "not-a-full-uuid" } });
      await waitFor(() => expect(requests[requests.length - 1]).toBeNull());

      fireEvent.change(filterResourceId, {
        target: { value: "11111111-1111-1111-1111-111111111111" },
      });
      await waitFor(() =>
        expect(requests[requests.length - 1]).toBe("11111111-1111-1111-1111-111111111111"),
      );
    });

    it("shows Clear filters only once a filter is active, and clearing resets every field", async () => {
      server.use(grantsHandler([GRANT]));
      renderWithProviders(<GrantsPage />);
      await waitFor(() => expect(screen.getByRole("table")).toBeInTheDocument());

      expect(screen.queryByRole("button", { name: "Clear filters" })).not.toBeInTheDocument();

      const filterGroup = document.getElementById("grant-filter-group") as HTMLSelectElement;
      fireEvent.change(filterGroup, { target: { value: "grp-1" } });
      const clearBtn = await screen.findByRole("button", { name: "Clear filters" });
      fireEvent.click(clearBtn);

      expect(screen.queryByRole("button", { name: "Clear filters" })).not.toBeInTheDocument();
      expect(filterGroup.value).toBe("");
    });
  });
});
