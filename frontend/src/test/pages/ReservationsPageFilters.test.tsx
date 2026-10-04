import { http, HttpResponse } from "msw";
import { render, screen, waitFor, fireEvent, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { describe, it, expect, vi, beforeEach } from "vitest";

// Search and filters on the Reservations page (issue #959). The preferences
// transport is mocked (the ReservationsPage.test.tsx pattern) so persistence is
// asserted on the queued PATCH payload.
const { patchPreferencesMock } = vi.hoisted(() => ({ patchPreferencesMock: vi.fn() }));
vi.mock("@/api/userProfile", () => ({
  getPreferences: vi.fn(),
  patchPreferences: patchPreferencesMock,
  resetPreferences: vi.fn(),
}));
vi.mock("@/components/reservations/ReservationDetailModal", () => ({
  ReservationDetailModal: () => null,
}));
vi.mock("react-hot-toast", () => ({ default: { success: vi.fn(), error: vi.fn() } }));

import { server } from "../mocks/server";
import { ReservationsPage } from "@/pages/ReservationsPage";
import { useAuthStore } from "@/stores/authStore";
import { usePreferencesStore } from "@/stores/preferencesStore";

const ME = "me-1";

function setRole(role: string) {
  useAuthStore.setState({
    user: {
      id: ME,
      email: "a@b.c",
      username: "u",
      is_active: true,
      role,
      created_at: "2026-01-01T00:00:00Z",
    },
  });
}

function row(n: number) {
  return {
    id: `0000000${n}-2222-3333-4444-555555555555`,
    user_id: ME,
    owner_name: "me",
    status: "ACTIVE",
    topology_id: null,
    topology_type: "PHYSICAL",
    device_ids: ["d-1"],
    start_time: "2026-06-01T00:00:00Z",
    end_time: "2026-06-02T00:00:00Z",
    purpose: `purpose ${n}`,
  };
}

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <ReservationsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function serve(items = [row(1)], total = 100, categories = ["qa_regression", "training"]) {
  const seen: URLSearchParams[] = [];
  server.use(
    http.get("/api/reservations/purpose-categories", () => HttpResponse.json({ categories })),
    http.get("/api/reservations/", ({ request }) => {
      seen.push(new URL(request.url).searchParams);
      return HttpResponse.json({ items, total, skip: 0, limit: 50 });
    }),
  );
  return seen;
}

const last = (seen: URLSearchParams[]) => seen[seen.length - 1];
const panel = () => within(screen.getByRole("region", { name: "Filters" }));
const selectionStatus = () => screen.getByRole("status", { name: "Selection" });

async function waitForCategories() {
  await waitFor(() =>
    expect(panel().getByRole("option", { name: "Training" })).toBeInTheDocument(),
  );
}

function lastSavedFilter() {
  const calls = patchPreferencesMock.mock.calls;
  for (let i = calls.length - 1; i >= 0; i--) {
    const f = calls[i][0].saved_filters?.reservations;
    if (f) return f;
  }
  return undefined;
}

beforeEach(() => {
  setRole("user");
  server.use(
    http.get("/api/inventory/devices", () =>
      HttpResponse.json({ items: [], total: 0, skip: 0, limit: 500 }),
    ),
  );
  patchPreferencesMock.mockReset();
  patchPreferencesMock.mockResolvedValue({});
  usePreferencesStore.getState().clear();
});

describe("ReservationsPage filters (issue #959)", () => {
  it("sends no filter params by default", async () => {
    const seen = serve();
    renderPage();
    await screen.findByText("purpose 1");
    for (const key of [
      "search",
      "status",
      "purpose_category",
      "starts_after",
      "starts_before",
      "ends_after",
      "ends_before",
    ]) {
      expect(last(seen).has(key)).toBe(false);
    }
  });

  it("the status filter sends status", async () => {
    const seen = serve();
    renderPage();
    await screen.findByText("purpose 1");
    fireEvent.change(panel().getByLabelText("Status"), { target: { value: "CANCELLED" } });
    await waitFor(() => expect(last(seen).getAll("status")).toEqual(["CANCELLED"]));
  });

  it("the purpose category filter sends the category or none", async () => {
    const seen = serve();
    renderPage();
    await waitForCategories();
    const select = panel().getByLabelText("Purpose category");
    fireEvent.change(select, { target: { value: "training" } });
    await waitFor(() => expect(last(seen).get("purpose_category")).toBe("training"));
    fireEvent.change(select, { target: { value: "none" } });
    await waitFor(() => expect(last(seen).get("purpose_category")).toBe("none"));
    expect(panel().getByRole("option", { name: "Unclassified" })).toBeInTheDocument();
  });

  it("each period sends its window from one anchor instant", async () => {
    const seen = serve();
    renderPage();
    await screen.findByText("purpose 1");
    const select = panel().getByLabelText("Period");

    fireEvent.change(select, { target: { value: "upcoming" } });
    await waitFor(() => expect(last(seen).has("starts_after")).toBe(true));
    expect([...last(seen).keys()].filter((k) => k.includes("_after") || k.includes("_before")))
      .toEqual(["starts_after"]);

    fireEvent.change(select, { target: { value: "current" } });
    await waitFor(() => expect(last(seen).has("ends_after")).toBe(true));
    expect(last(seen).get("starts_before")).toBe(last(seen).get("ends_after"));
    expect(last(seen).has("starts_after")).toBe(false);

    fireEvent.change(select, { target: { value: "past" } });
    await waitFor(() => expect(last(seen).has("ends_before")).toBe(true));
    expect(last(seen).has("starts_before")).toBe(false);
    expect(Number.isNaN(Date.parse(last(seen).get("ends_before") ?? ""))).toBe(false);
  });

  it("paging keeps the period anchor fixed", async () => {
    const seen = serve();
    renderPage();
    await screen.findByText("purpose 1");
    fireEvent.change(panel().getByLabelText("Period"), { target: { value: "past" } });
    await waitFor(() => expect(last(seen).has("ends_before")).toBe(true));
    const anchor = last(seen).get("ends_before");
    fireEvent.click(screen.getByText("Next"));
    await waitFor(() => expect(last(seen).get("skip")).toBe("50"));
    expect(last(seen).get("ends_before")).toBe(anchor);
  });

  it("the search is debounced and sent trimmed", async () => {
    const seen = serve();
    renderPage();
    await screen.findByText("purpose 1");
    fireEvent.change(panel().getByLabelText("Search reservations"), {
      target: { value: "  lab run " },
    });
    await waitFor(() => expect(last(seen).get("search")).toBe("lab run"), { timeout: 2000 });
  });

  it("filters compose with sort and the admin all toggle", async () => {
    setRole("admin");
    const seen = serve();
    renderPage();
    await screen.findByText("purpose 1");
    fireEvent.click(panel().getByLabelText("All reservations"));
    fireEvent.click(screen.getByRole("button", { name: "Status" }));
    fireEvent.change(panel().getByLabelText("Status"), { target: { value: "FAILED" } });
    await waitFor(() => {
      const p = last(seen);
      expect(p.get("all")).toBe("true");
      expect(p.get("sort_by")).toBe("status");
      expect(p.get("sort_dir")).toBe("asc");
      expect(p.getAll("status")).toEqual(["FAILED"]);
    });
  });

  it("keeps the All reservations toggle's accessible name, inside the panel", async () => {
    setRole("admin");
    serve();
    renderPage();
    await screen.findByText("purpose 1");
    const toggle = screen.getByRole("checkbox", { name: "All reservations" });
    expect(screen.getByRole("region", { name: "Filters" })).toContainElement(toggle);
  });

  it("any filter change returns to page one", async () => {
    const seen = serve();
    renderPage();
    await screen.findByText("purpose 1");
    for (const [label, value] of [
      ["Status", "ACTIVE"],
      ["Period", "past"],
      ["Purpose category", "none"],
    ] as const) {
      fireEvent.click(screen.getByText("Next"));
      await waitFor(() => expect(last(seen).get("skip")).toBe("50"));
      fireEvent.change(panel().getByLabelText(label), { target: { value } });
      await waitFor(() => expect(last(seen).get("skip")).toBe("0"));
    }
    fireEvent.click(screen.getByText("Next"));
    await waitFor(() => expect(last(seen).get("skip")).toBe("50"));
    fireEvent.change(panel().getByLabelText("Search reservations"), { target: { value: "x" } });
    await waitFor(() => expect(last(seen).get("skip")).toBe("0"), { timeout: 2000 });
  });

  it("clears the selection on a filter change", async () => {
    serve();
    renderPage();
    for (const [label, value] of [
      ["Status", "ACTIVE"],
      ["Period", "upcoming"],
      ["Purpose category", "none"],
      ["Search reservations", "purpose"],
    ] as const) {
      fireEvent.click(await screen.findByRole("checkbox", { name: /Select reservation/ }));
      expect(selectionStatus()).toHaveTextContent("1 selected");
      fireEvent.change(panel().getByLabelText(label), { target: { value } });
      await waitFor(() => expect(selectionStatus()).not.toHaveTextContent("selected"), {
        timeout: 2000,
      });
    }
  });

  it("persists every choice in savedFilters.reservations", async () => {
    serve();
    renderPage();
    await waitForCategories();
    fireEvent.change(panel().getByLabelText("Status"), { target: { value: "PENDING" } });
    fireEvent.change(panel().getByLabelText("Purpose category"), {
      target: { value: "qa_regression" },
    });
    fireEvent.change(panel().getByLabelText("Period"), { target: { value: "current" } });
    fireEvent.change(panel().getByLabelText("Search reservations"), { target: { value: "lab" } });
    await waitFor(
      () =>
        expect(lastSavedFilter()).toEqual({
          search: "lab",
          status: "PENDING",
          purpose_category: "qa_regression",
          period: "current",
        }),
      { timeout: 2000 },
    );
  });

  it("applies a saved filter on load", async () => {
    usePreferencesStore.setState({
      savedFilters: {
        reservations: {
          search: "saved",
          status: "COMPLETED",
          purpose_category: "training",
          period: "past",
        },
      },
    });
    const seen = serve();
    renderPage();
    await waitFor(() => expect(seen.length).toBeGreaterThan(0));
    const first = seen[0];
    expect(first.get("search")).toBe("saved");
    expect(first.getAll("status")).toEqual(["COMPLETED"]);
    expect(first.get("purpose_category")).toBe("training");
    expect(first.has("ends_before")).toBe(true);
    expect(panel().getByLabelText("Search reservations")).toHaveValue("saved");
  });

  it("never sends a stale saved value", async () => {
    usePreferencesStore.setState({
      savedFilters: {
        reservations: {
          search: "",
          status: "ARCHIVED",
          purpose_category: "dropped_category",
          period: "someday",
        },
      },
    });
    const seen = serve();
    renderPage();
    await waitFor(() => expect(seen.length).toBeGreaterThan(0));
    await waitForCategories();
    for (const p of seen) {
      expect(p.has("status")).toBe(false);
      expect(p.has("purpose_category")).toBe(false);
      expect([...p.keys()].some((k) => k.endsWith("_after") || k.endsWith("_before"))).toBe(
        false,
      );
    }
    expect(panel().getByLabelText("Status")).toHaveValue("");
    expect(panel().getByLabelText("Purpose category")).toHaveValue("");
    expect(panel().getByLabelText("Period")).toHaveValue("");
  });

  it("shows a filtered-empty state whose Clear control resets every filter", async () => {
    const seen = serve([], 0);
    renderPage();
    expect(await screen.findByText("No reservations yet")).toBeInTheDocument();
    fireEvent.change(panel().getByLabelText("Status"), { target: { value: "FAILED" } });
    expect(
      await screen.findByText(/No reservations match the current filters/),
    ).toBeInTheDocument();
    expect(screen.queryByText("No reservations yet")).not.toBeInTheDocument();

    const clears = screen.getAllByRole("button", { name: "Clear filters" });
    expect(clears).toHaveLength(2); // the panel's and the empty state's
    fireEvent.click(clears[clears.length - 1]);
    await waitFor(() => expect(last(seen).has("status")).toBe(false));
    expect(await screen.findByText("No reservations yet")).toBeInTheDocument();
    expect(panel().getByLabelText("Status")).toHaveValue("");
    await waitFor(() => expect(lastSavedFilter()).toEqual({ search: "" }));
  });
});
