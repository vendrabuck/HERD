import { http, HttpResponse } from "msw";
import { act, render, screen, waitFor, fireEvent, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type { ReactNode } from "react";
import { describe, it, expect, vi, beforeAll, beforeEach } from "vitest";

/**
 * Topologies page list controls (issue #958): search, Owner filter, sortable
 * headings, persistence, and multi-select delete, against the real API client
 * through msw so every assertion is on the request the page actually sends.
 */

beforeAll(() => {
  HTMLDialogElement.prototype.showModal = vi.fn(function (this: HTMLDialogElement) {
    this.open = true;
  });
  HTMLDialogElement.prototype.close = vi.fn(function (this: HTMLDialogElement) {
    this.open = false;
  });
});

const { patchPreferencesMock } = vi.hoisted(() => ({ patchPreferencesMock: vi.fn() }));
vi.mock("@/api/userProfile", () => ({
  getPreferences: vi.fn(),
  patchPreferences: patchPreferencesMock,
  resetPreferences: vi.fn(),
}));

const { toastSuccess, toastError } = vi.hoisted(() => ({
  toastSuccess: vi.fn(),
  toastError: vi.fn(),
}));
vi.mock("react-hot-toast", () => ({ default: { success: toastSuccess, error: toastError } }));

vi.mock("@/api/bulk", () => ({ exportTopologies: vi.fn(), importTopologies: vi.fn() }));

import { server } from "../mocks/server";
import { TopologyPage } from "@/pages/TopologyPage";
import { useAuthStore } from "@/stores/authStore";
import { usePreferencesStore } from "@/stores/preferencesStore";

const ME = "me-id";

function setUser(role: string, id = ME) {
  useAuthStore.setState({
    user: {
      id,
      email: "a@b.c",
      username: "u",
      is_active: true,
      role,
      created_at: "2026-01-01T00:00:00Z",
    },
  });
}

const topo = (n: number, createdBy = ME) => ({
  id: `topo-${n}`,
  name: `Topo ${n}`,
  created_by: createdBy,
  owner_name: createdBy === ME ? "me" : "someone",
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-02T00:00:00Z",
  canvas_data: null,
});

type Row = ReturnType<typeof topo>;

/** Serve `items` for every list request and record each request's params. */
function serveList(items: Row[], total = items.length) {
  const seen: URLSearchParams[] = [];
  server.use(
    http.get("/api/cabling/topologies", ({ request }) => {
      seen.push(new URL(request.url).searchParams);
      return HttpResponse.json({ items, total, skip: 0, limit: 50 });
    }),
  );
  return seen;
}

function renderPage(
  client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  }),
  node: ReactNode = <TopologyPage />,
) {
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>{node}</MemoryRouter>
    </QueryClientProvider>,
  );
}

const last = (seen: URLSearchParams[]) => seen[seen.length - 1];
const rowBox = (n: number) => screen.getByRole("checkbox", { name: `Select topology Topo ${n}` });
const selectAllBox = () =>
  screen.getByRole("checkbox", { name: "Select all topologies on this page" });
const header = (name: string) => screen.getByRole("columnheader", { name });
const selectionBar = () => screen.getByRole("status", { name: "Selection" });
const lastPatch = () =>
  patchPreferencesMock.mock.calls[patchPreferencesMock.mock.calls.length - 1][0];

beforeEach(() => {
  setUser("user");
  toastSuccess.mockReset();
  toastError.mockReset();
  patchPreferencesMock.mockReset();
  patchPreferencesMock.mockResolvedValue({
    user_id: "u",
    saved_filters: {},
    page_sizes: {},
    extras: {},
    updated_at: "",
  });
  usePreferencesStore.getState().clear();
});

describe("sort (issue #958)", () => {
  it("sends no sort params by default and shows Updated as the active descending order", async () => {
    const seen = serveList([topo(1)]);
    renderPage();
    await screen.findByText("Topo 1");
    const params = last(seen);
    expect(params.get("sort_by")).toBeNull();
    expect(params.get("sort_dir")).toBeNull();
    expect(params.get("search")).toBeNull();
    expect(params.get("owner")).toBeNull();
    expect(header("Updated")).toHaveAttribute("aria-sort", "descending");
    for (const name of ["Name", "Owner", "Created"]) {
      expect(header(name)).toHaveAttribute("aria-sort", "none");
    }
  });

  it("a heading cycles ascending, descending, then back to the default, resetting to page 1", async () => {
    const seen = serveList([topo(1)], 120);
    renderPage();
    await screen.findByText("Topo 1");
    fireEvent.click(screen.getByRole("button", { name: /next/i }));
    await waitFor(() => expect(last(seen).get("skip")).toBe("50"));

    fireEvent.click(screen.getByRole("button", { name: "Name" }));
    await waitFor(() => expect(last(seen).get("sort_by")).toBe("name"));
    expect(last(seen).get("sort_dir")).toBe("asc");
    expect(last(seen).get("skip")).toBe("0");
    expect(header("Name")).toHaveAttribute("aria-sort", "ascending");
    expect(header("Updated")).toHaveAttribute("aria-sort", "none");

    fireEvent.click(screen.getByRole("button", { name: "Name" }));
    await waitFor(() => expect(last(seen).get("sort_dir")).toBe("desc"));
    expect(header("Name")).toHaveAttribute("aria-sort", "descending");

    fireEvent.click(screen.getByRole("button", { name: "Name" }));
    await waitFor(() => expect(last(seen).get("sort_by")).toBeNull());
    expect(header("Updated")).toHaveAttribute("aria-sort", "descending");
  });

  it.each([
    ["Owner", "owner_name"],
    ["Created", "created_at"],
    ["Updated", "updated_at"],
  ])("%s sorts by %s", async (label, field) => {
    const seen = serveList([topo(1)]);
    renderPage();
    await screen.findByText("Topo 1");
    fireEvent.click(screen.getByRole("button", { name: label }));
    await waitFor(() => expect(last(seen).get("sort_by")).toBe(field));
    expect(last(seen).get("sort_dir")).toBe("asc");
  });

  it("persists the choice under extras sort:topologies", async () => {
    serveList([topo(1)]);
    renderPage();
    await screen.findByText("Topo 1");
    fireEvent.click(screen.getByRole("button", { name: "Created" }));
    await waitFor(() => expect(patchPreferencesMock).toHaveBeenCalled());
    expect(lastPatch().extras).toEqual({
      "sort:topologies": { sortBy: "created_at", sortDir: "asc" },
    });
  });

  it("applies a persisted sort on load", async () => {
    usePreferencesStore.setState({
      extras: { "sort:topologies": { sortBy: "owner_name", sortDir: "desc" } },
    });
    const seen = serveList([topo(1)]);
    renderPage();
    await waitFor(() => expect(seen.length).toBeGreaterThan(0));
    expect(last(seen).get("sort_by")).toBe("owner_name");
    expect(last(seen).get("sort_dir")).toBe("desc");
    expect(header("Owner")).toHaveAttribute("aria-sort", "descending");
  });

  it("a stale persisted sort field falls back to the default and is never sent", async () => {
    usePreferencesStore.setState({
      extras: { "sort:topologies": { sortBy: "status", sortDir: "asc" } },
    });
    const seen = serveList([topo(1)]);
    renderPage();
    await screen.findByText("Topo 1");
    expect(seen.every((p) => p.get("sort_by") === null && p.get("sort_dir") === null)).toBe(true);
    expect(header("Updated")).toHaveAttribute("aria-sort", "descending");
  });
});

describe("search and Owner filter (issue #958)", () => {
  it("search is debounced, trimmed, sent, resets to page 1, and persisted", async () => {
    const seen = serveList([topo(1)], 120);
    renderPage();
    await screen.findByText("Topo 1");
    fireEvent.click(screen.getByRole("button", { name: /next/i }));
    await waitFor(() => expect(last(seen).get("skip")).toBe("50"));

    fireEvent.change(screen.getByLabelText("Search topologies"), {
      target: { value: "  core " },
    });
    await waitFor(() => expect(last(seen).get("search")).toBe("core"));
    expect(last(seen).get("skip")).toBe("0");
    await waitFor(() =>
      expect(lastPatch().saved_filters).toEqual({ topologies: { search: "  core " } }),
    );
  });

  it("a whitespace-only search sends nothing", async () => {
    const seen = serveList([topo(1)]);
    renderPage();
    await screen.findByText("Topo 1");
    fireEvent.change(screen.getByLabelText("Search topologies"), { target: { value: "   " } });
    await waitFor(() => expect(patchPreferencesMock).toHaveBeenCalled());
    expect(seen.every((p) => p.get("search") === null)).toBe(true);
  });

  it("Owner Mine sends owner=mine and persists it; All removes it", async () => {
    const seen = serveList([topo(1)]);
    renderPage();
    await screen.findByText("Topo 1");
    fireEvent.change(screen.getByLabelText("Owner"), { target: { value: "mine" } });
    await waitFor(() => expect(last(seen).get("owner")).toBe("mine"));
    await waitFor(() =>
      expect(lastPatch().saved_filters).toEqual({ topologies: { search: "", owner: "mine" } }),
    );
    fireEvent.change(screen.getByLabelText("Owner"), { target: { value: "all" } });
    await waitFor(() => expect(last(seen).get("owner")).toBeNull());
  });

  it("applies saved filters on load", async () => {
    usePreferencesStore.setState({
      savedFilters: { topologies: { search: "edge", owner: "mine" } },
    });
    const seen = serveList([topo(1)]);
    renderPage();
    await waitFor(() => expect(seen.length).toBeGreaterThan(0));
    expect(last(seen).get("search")).toBe("edge");
    expect(last(seen).get("owner")).toBe("mine");
    expect(screen.getByLabelText("Search topologies")).toHaveValue("edge");
    expect(screen.getByLabelText("Owner")).toHaveValue("mine");
  });

  // Issue #982: the preferences arrive after the first list request, as on a
  // full page reload. This page already has the #959 shape; these pin it.
  it("a saved search that loads after mount applies at once and survives an Owner change", async () => {
    const seen = serveList([topo(1)]);
    renderPage();
    await screen.findByText("Topo 1");
    act(() => {
      usePreferencesStore.setState({ savedFilters: { topologies: { search: "late" } } });
    });
    // Changed at once, before any 300 ms debounce could run.
    fireEvent.change(screen.getByLabelText("Owner"), { target: { value: "mine" } });
    await waitFor(() => expect(last(seen).get("owner")).toBe("mine"));
    expect(last(seen).get("search")).toBe("late");
    expect(usePreferencesStore.getState().savedFilters.topologies).toEqual({
      search: "late",
      owner: "mine",
    });
    expect(screen.getByLabelText("Search topologies")).toHaveValue("late");
  });

  it("a saved search that loads after mount survives a sort click and Clear filters clears it", async () => {
    const seen = serveList([topo(1)]);
    renderPage();
    await screen.findByText("Topo 1");
    act(() => {
      usePreferencesStore.setState({ savedFilters: { topologies: { search: "late" } } });
    });
    fireEvent.click(screen.getByRole("button", { name: "Name" }));
    await waitFor(() => expect(last(seen).get("sort_by")).toBe("name"));
    expect(last(seen).get("search")).toBe("late");
    await act(async () => {
      await new Promise((r) => setTimeout(r, 450));
    });
    expect(usePreferencesStore.getState().savedFilters.topologies).toEqual({ search: "late" });

    fireEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    await waitFor(() => expect(last(seen).get("search")).toBeNull());
    await act(async () => {
      await new Promise((r) => setTimeout(r, 450));
    });
    expect(usePreferencesStore.getState().savedFilters.topologies).toEqual({ search: "" });
    expect(last(seen).get("search")).toBeNull();
  });

  it("a stale saved owner falls back to All and is never sent", async () => {
    usePreferencesStore.setState({ savedFilters: { topologies: { owner: "theirs" } } });
    const seen = serveList([topo(1)]);
    renderPage();
    await screen.findByText("Topo 1");
    expect(seen.every((p) => p.get("owner") === null)).toBe(true);
    expect(screen.getByLabelText("Owner")).toHaveValue("all");
    expect(screen.queryByRole("button", { name: "Clear filters" })).not.toBeInTheDocument();
  });

  it("Clear filters resets search and owner and persists the cleared state", async () => {
    usePreferencesStore.setState({
      savedFilters: { topologies: { search: "edge", owner: "mine" } },
    });
    const seen = serveList([topo(1)]);
    renderPage();
    await screen.findByText("Topo 1");
    fireEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    await waitFor(() => {
      expect(last(seen).get("search")).toBeNull();
      expect(last(seen).get("owner")).toBeNull();
    });
    expect(screen.getByLabelText("Search topologies")).toHaveValue("");
    await waitFor(() =>
      expect(lastPatch().saved_filters).toEqual({ topologies: { search: "" } }),
    );
  });

  it("an empty filtered result says so instead of the first-run empty state", async () => {
    usePreferencesStore.setState({ savedFilters: { topologies: { search: "zzz" } } });
    serveList([]);
    renderPage();
    expect(await screen.findByText("No topologies match these filters.")).toBeInTheDocument();
  });
});

describe("selection (issue #958)", () => {
  it("a row checkbox selects without navigating, and select-all takes the page", async () => {
    serveList([topo(1), topo(2), topo(3)]);
    renderPage();
    await screen.findByText("Topo 1");
    fireEvent.click(rowBox(1));
    expect(rowBox(1)).toBeChecked();
    expect(selectionBar()).toHaveTextContent("1 selected");
    expect(selectAllBox()).not.toBeChecked();
    expect((selectAllBox() as HTMLInputElement).indeterminate).toBe(true);
    fireEvent.click(selectAllBox());
    expect(selectionBar()).toHaveTextContent("3 selected");
    fireEvent.click(selectAllBox());
    expect(selectionBar()).not.toHaveTextContent("selected");
  });

  it("Clear selection empties it", async () => {
    serveList([topo(1)]);
    renderPage();
    await screen.findByText("Topo 1");
    fireEvent.click(rowBox(1));
    fireEvent.click(screen.getByRole("button", { name: "Clear selection" }));
    expect(rowBox(1)).not.toBeChecked();
  });

  async function selectedThen(change: () => void, seen: URLSearchParams[], key: string) {
    await screen.findByText("Topo 1");
    fireEvent.click(rowBox(1));
    expect(rowBox(1)).toBeChecked();
    const before = seen.length;
    change();
    await waitFor(() => expect(seen.length).toBeGreaterThan(before));
    await waitFor(() => expect(last(seen).toString()).toContain(key));
    expect(rowBox(1)).not.toBeChecked();
    expect(selectionBar()).toHaveTextContent("");
  }

  it("clears on a page change", async () => {
    const seen = serveList([topo(1)], 120);
    renderPage();
    await selectedThen(
      () => fireEvent.click(screen.getByRole("button", { name: /next/i })),
      seen,
      "skip=50",
    );
  });

  it("clears on a sort change", async () => {
    const seen = serveList([topo(1)]);
    renderPage();
    await selectedThen(
      () => fireEvent.click(screen.getByRole("button", { name: "Name" })),
      seen,
      "sort_by=name",
    );
  });

  it("clears on a search change", async () => {
    const seen = serveList([topo(1)]);
    renderPage();
    await selectedThen(
      () =>
        fireEvent.change(screen.getByLabelText("Search topologies"), { target: { value: "To" } }),
      seen,
      "search=To",
    );
  });

  it("clears on an Owner filter change", async () => {
    const seen = serveList([topo(1)]);
    renderPage();
    await selectedThen(
      () => fireEvent.change(screen.getByLabelText("Owner"), { target: { value: "mine" } }),
      seen,
      "owner=mine",
    );
  });
});

describe("delete gate and bulk delete (issue #958)", () => {
  it("the row Delete and the bulk eligibility share one rule: a non-admin non-owner gets neither", async () => {
    serveList([topo(1), topo(2, "other-id")]);
    renderPage();
    await screen.findByText("Topo 1");
    expect(screen.getByRole("button", { name: "Delete topology Topo 1" })).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Delete topology Topo 2" }),
    ).not.toBeInTheDocument();
    fireEvent.click(rowBox(2));
    const del = screen.getByRole("button", { name: "Delete selected" });
    expect(del).toBeDisabled();
    expect(del).toHaveAccessibleDescription(
      "None of the selected topologies can be deleted: 1 not yours.",
    );
  });

  it.each(["admin", "superadmin"])("a %s may delete another user's topology", async (role) => {
    setUser(role);
    serveList([topo(2, "other-id")]);
    renderPage();
    await screen.findByText("Topo 2");
    expect(screen.getByRole("button", { name: "Delete topology Topo 2" })).toBeInTheDocument();
    fireEvent.click(rowBox(2));
    expect(screen.getByRole("button", { name: "Delete selected" })).toBeEnabled();
  });

  it("the confirmation counts what will be deleted and what is skipped, and Keep aborts", async () => {
    serveList([topo(1), topo(2, "other-id"), topo(3)]);
    const deletes: string[] = [];
    server.use(
      http.delete("/api/cabling/topologies/:id", ({ params }) => {
        deletes.push(String(params.id));
        return new HttpResponse(null, { status: 204 });
      }),
    );
    renderPage();
    await screen.findByText("Topo 1");
    fireEvent.click(selectAllBox());
    fireEvent.click(screen.getByRole("button", { name: "Delete selected" }));
    const dialog = screen.getByText("Delete Topologies").closest("dialog")!;
    expect(dialog).toHaveAttribute("open");
    expect(
      within(dialog).getByText(
        "Delete 2 of the 3 selected topologies? This permanently deletes them and their " +
          "canvas data and cannot be undone. 1 will be skipped: not yours.",
      ),
    ).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "Keep topologies" }));
    expect(dialog).not.toHaveAttribute("open");
    expect(deletes).toEqual([]);

    fireEvent.click(screen.getByRole("button", { name: "Delete selected" }));
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete 2 topologies" }));
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith("Deleted 2 topologies"));
    // Only the eligible rows are sent; the skipped row stays selected.
    expect(deletes.sort()).toEqual(["topo-1", "topo-3"]);
    expect(rowBox(2)).toBeChecked();
  });

  it("a partial failure keeps the failed row selected with the server's reason; invalidates once", async () => {
    serveList([topo(1), topo(2), topo(3)]);
    server.use(
      http.delete("/api/cabling/topologies/:id", ({ params }) =>
        params.id === "topo-2"
          ? HttpResponse.json({ detail: "Not authorized to delete this topology" }, { status: 403 })
          : new HttpResponse(null, { status: 204 }),
      ),
    );
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    const invalidate = vi.spyOn(client, "invalidateQueries");
    renderPage(client);
    await screen.findByText("Topo 1");
    fireEvent.click(selectAllBox());
    fireEvent.click(screen.getByRole("button", { name: "Delete selected" }));
    fireEvent.click(await screen.findByRole("button", { name: "Delete 3 topologies" }));

    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith(
        "Deleted 2, failed 1: Not authorized to delete this topology",
      ),
    );
    expect(toastSuccess).not.toHaveBeenCalled();
    expect(rowBox(2)).toBeChecked();
    expect(rowBox(1)).not.toBeChecked();
    expect(rowBox(3)).not.toBeChecked();
    expect(selectionBar()).toHaveTextContent("1 selected");
    expect(
      screen.getByText("Not deleted: Not authorized to delete this topology"),
    ).toBeInTheDocument();
    expect(invalidate).toHaveBeenCalledTimes(1);
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["topologies"] });

    // Deselecting the failed row drops its note with it.
    fireEvent.click(rowBox(2));
    expect(screen.queryByText(/Not deleted/)).not.toBeInTheDocument();
  });

  it("different failure reasons give counts only and keep every failed row", async () => {
    serveList([topo(1), topo(2)]);
    server.use(
      http.delete("/api/cabling/topologies/:id", ({ params }) =>
        HttpResponse.json(
          { detail: params.id === "topo-1" ? "one reason" : "another reason" },
          { status: 409 },
        ),
      ),
    );
    renderPage();
    await screen.findByText("Topo 1");
    fireEvent.click(selectAllBox());
    fireEvent.click(screen.getByRole("button", { name: "Delete selected" }));
    fireEvent.click(await screen.findByRole("button", { name: "Delete 2 topologies" }));
    await waitFor(() => expect(toastError).toHaveBeenCalledWith("Deleted 0, failed 2"));
    expect(rowBox(1)).toBeChecked();
    expect(rowBox(2)).toBeChecked();
    expect(screen.getByText("Not deleted: one reason")).toBeInTheDocument();
    expect(screen.getByText("Not deleted: another reason")).toBeInTheDocument();
  });

  it("disables the bulk buttons while the fan-out is in flight", async () => {
    serveList([topo(1)]);
    let release: () => void = () => {};
    server.use(
      http.delete(
        "/api/cabling/topologies/:id",
        () =>
          new Promise<Response>((resolve) => {
            release = () => resolve(new HttpResponse(null, { status: 204 }));
          }),
      ),
    );
    renderPage();
    await screen.findByText("Topo 1");
    fireEvent.click(rowBox(1));
    fireEvent.click(screen.getByRole("button", { name: "Delete selected" }));
    fireEvent.click(await screen.findByRole("button", { name: "Delete 1 topology" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Delete selected" })).toBeDisabled());
    expect(screen.getByRole("button", { name: "Clear selection" })).toBeDisabled();
    release();
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith("Deleted 1 topology"));
  });
});
