import { http, HttpResponse } from "msw";
import { render, screen, waitFor, fireEvent, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";

// Admin purpose-review page (issue #646 phase 2, ADR 0013 point 10).

const toastSuccess = vi.fn();
const toastError = vi.fn();
vi.mock("react-hot-toast", () => ({
  default: { success: (m: string) => toastSuccess(m), error: (m: string) => toastError(m) },
}));

const AUTH_STATE = {
  user: { id: "admin-1", role: "admin", username: "admin", email: "a@b.c" },
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
import { PurposeReviewPage } from "@/pages/admin/PurposeReviewPage";

function renderWithProviders(node: ReactNode) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={client}>{node}</QueryClientProvider>);
}

const SUGGESTION_QA = {
  distribution: [
    { category: "qa_regression", probability: 0.8 },
    { category: "training", probability: 0.15 },
    { category: "other", probability: 0.05 },
  ],
  top_category: "qa_regression",
  pass: "end",
  model: "test-model",
  rationale: "matches regression keywords",
  generated_at: "2026-06-02T01:00:00Z",
  signals_used: ["purpose_text"],
};

const SUGGESTION_DEMO = {
  distribution: [{ category: "customer_demo_poc", probability: 0.9 }],
  top_category: "customer_demo_poc",
  pass: "end",
  model: "test-model",
  rationale: "mentions a customer demo",
  generated_at: "2026-06-04T01:00:00Z",
  signals_used: ["purpose_text"],
};

const ITEM_1 = {
  reservation_id: "r-1",
  user_id: "u-1",
  purpose: "regression suite",
  start_time: "2026-06-01T00:00:00Z",
  end_time: "2026-06-02T00:00:00Z",
  status: "COMPLETED",
  purpose_category: null,
  purpose_suggestion: SUGGESTION_QA,
  purpose_suggested_at: "2026-06-02T01:00:00Z",
  device_count: 3,
};

const ITEM_2 = {
  reservation_id: "r-2",
  user_id: "u-2",
  purpose: "demo for customer",
  start_time: "2026-06-03T00:00:00Z",
  end_time: "2026-06-04T00:00:00Z",
  status: "COMPLETED",
  purpose_category: "training",
  purpose_suggestion: SUGGESTION_DEMO,
  purpose_suggested_at: "2026-06-04T01:00:00Z",
  device_count: 1,
};

function mockUsers() {
  server.use(
    http.get("/api/auth/users", () =>
      HttpResponse.json({
        items: [
          {
            id: "u-1",
            username: "alice",
            email: "a@b.c",
            is_active: true,
            role: "user",
            created_at: "2026-01-01T00:00:00Z",
          },
          {
            id: "u-2",
            username: "bob",
            email: "b@b.c",
            is_active: true,
            role: "user",
            created_at: "2026-01-01T00:00:00Z",
          },
        ],
        total: 2,
        skip: 0,
        limit: 500,
      }),
    ),
  );
}

function mockCategories() {
  server.use(
    http.get("/api/reservations/purpose-categories", () =>
      HttpResponse.json({
        categories: ["qa_regression", "training", "other", "customer_demo_poc"],
      }),
    ),
  );
}

function mockReview(
  items: Array<Record<string, unknown>> = [ITEM_1, ITEM_2],
  totalOverride?: number,
) {
  server.use(
    http.get("/api/reservations/admin/purpose-review", ({ request }) => {
      const url = new URL(request.url);
      const category = url.searchParams.get("category");
      const filtered = category
        ? items.filter(
            (i) =>
              (i.purpose_suggestion as { top_category?: string } | null)?.top_category ===
              category,
          )
        : items;
      return HttpResponse.json({
        items: filtered,
        total: totalOverride ?? filtered.length,
        skip: 0,
        limit: 20,
      });
    }),
  );
}

beforeEach(() => {
  toastSuccess.mockClear();
  toastError.mockClear();
  mockUsers();
  mockCategories();
});

describe("PurposeReviewPage", () => {
  it("groups items by top suggested category, shows owner names and counts", async () => {
    mockReview();
    renderWithProviders(<PurposeReviewPage />);

    await screen.findByText("regression suite");
    // "QA and regression" also appears as an <option> in every category
    // select (the top filter and each row's "Accept as..." picker), so
    // narrow to the one live inside a group's <summary>.
    const qaHeading = screen
      .getAllByText("QA and regression")
      .find((el) => el.closest("summary")) as HTMLElement;
    const qaGroup = qaHeading.closest("summary") as HTMLElement;
    expect(within(qaGroup).getByText("1")).toBeInTheDocument();

    expect(
      screen.getAllByText("Customer demo or POC").some((el) => el.closest("summary")),
    ).toBe(true);
    // The owner-name lookup is a second, independent query (useAllUsers); it
    // can resolve after the review list itself. Regex matchers since the
    // name is only part of a row's combined "owner - dates - devices" text.
    expect(await screen.findByText(/alice/)).toBeInTheDocument();
    expect(await screen.findByText(/bob/)).toBeInTheDocument();
    expect(screen.getByText("regression suite")).toBeInTheDocument();
    expect(screen.getByText("demo for customer")).toBeInTheDocument();
  });

  it("shows the confirmed category tag on a row that already has one", async () => {
    mockReview();
    renderWithProviders(<PurposeReviewPage />);
    const row = (await screen.findByText("demo for customer")).closest("li") as HTMLElement;
    // ITEM_2 carries purpose_category: "training" alongside its suggestion;
    // scope to the tag <span> since the row's own "Accept a different
    // category" select also lists "Training" as an option.
    const tag = within(row)
      .getAllByText("Training")
      .find((el) => el.tagName === "SPAN") as HTMLElement;
    expect(tag).toBeInTheDocument();
  });

  it("shows the distribution as percentage chips with the rationale as a title tooltip", async () => {
    mockReview();
    renderWithProviders(<PurposeReviewPage />);
    await screen.findByText("regression suite");
    const chip = screen.getByText("QA and regression 80%");
    expect(chip).toBeInTheDocument();
    expect(screen.getByText("Training 15%")).toBeInTheDocument();
    expect(chip.parentElement).toHaveAttribute("title", SUGGESTION_QA.rationale);
  });

  it("filters the list by category, re-querying the server", async () => {
    mockReview();
    renderWithProviders(<PurposeReviewPage />);
    await screen.findByText("regression suite");

    fireEvent.change(screen.getByLabelText("Category"), {
      target: { value: "customer_demo_poc" },
    });

    await waitFor(() => expect(screen.queryByText("regression suite")).not.toBeInTheDocument());
    expect(screen.getByText("demo for customer")).toBeInTheDocument();
  });

  it("shows the empty state when there is nothing to review", async () => {
    mockReview([]);
    renderWithProviders(<PurposeReviewPage />);
    expect(await screen.findByText("Nothing to review")).toBeInTheDocument();
  });

  it("accepts the top category: removes the row optimistically and posts purpose_category null", async () => {
    mockReview();
    let acceptedBody: unknown = null;
    server.use(
      http.post("/api/reservations/admin/purpose-review/:id/accept", async ({ request }) => {
        acceptedBody = await request.json();
        return HttpResponse.json({ ...ITEM_1, purpose_category: "qa_regression" });
      }),
    );
    renderWithProviders(<PurposeReviewPage />);
    await screen.findByText("regression suite");

    const row = screen.getByText("regression suite").closest("li") as HTMLElement;
    fireEvent.click(within(row).getByRole("button", { name: "Accept" }));

    // Optimistic: gone immediately, before the server has responded.
    expect(screen.queryByText("regression suite")).not.toBeInTheDocument();

    await waitFor(() => expect(acceptedBody).toEqual({ purpose_category: null }));
    expect(toastSuccess).toHaveBeenCalledWith("Accepted");
  });

  it("accepts a different category from the row's select", async () => {
    mockReview();
    let acceptedBody: unknown = null;
    server.use(
      http.post("/api/reservations/admin/purpose-review/:id/accept", async ({ request }) => {
        acceptedBody = await request.json();
        return HttpResponse.json({ ...ITEM_1, purpose_category: "other" });
      }),
    );
    renderWithProviders(<PurposeReviewPage />);
    await screen.findByText("regression suite");

    const row = screen.getByText("regression suite").closest("li") as HTMLElement;
    fireEvent.change(within(row).getByLabelText("Accept a different category"), {
      target: { value: "other" },
    });

    expect(screen.queryByText("regression suite")).not.toBeInTheDocument();
    await waitFor(() => expect(acceptedBody).toEqual({ purpose_category: "other" }));
  });

  it("dismisses a row: removes it optimistically and calls the dismiss endpoint", async () => {
    mockReview();
    let dismissCalled = false;
    server.use(
      http.post("/api/reservations/admin/purpose-review/:id/dismiss", () => {
        dismissCalled = true;
        return HttpResponse.json({ ...ITEM_1 });
      }),
    );
    renderWithProviders(<PurposeReviewPage />);
    await screen.findByText("regression suite");

    const row = screen.getByText("regression suite").closest("li") as HTMLElement;
    fireEvent.click(within(row).getByRole("button", { name: "Dismiss" }));

    expect(screen.queryByText("regression suite")).not.toBeInTheDocument();
    await waitFor(() => expect(dismissCalled).toBe(true));
    expect(toastSuccess).toHaveBeenCalledWith("Dismissed");
  });

  it("reverts an optimistic accept and toasts the server error on failure", async () => {
    mockReview();
    server.use(
      http.post("/api/reservations/admin/purpose-review/:id/accept", () =>
        HttpResponse.json({ detail: "reservation already confirmed" }, { status: 409 }),
      ),
    );
    renderWithProviders(<PurposeReviewPage />);
    await screen.findByText("regression suite");

    const row = screen.getByText("regression suite").closest("li") as HTMLElement;
    fireEvent.click(within(row).getByRole("button", { name: "Accept" }));
    expect(screen.queryByText("regression suite")).not.toBeInTheDocument();

    await waitFor(() => expect(screen.getByText("regression suite")).toBeInTheDocument());
    expect(toastError).toHaveBeenCalledWith("reservation already confirmed");
  });

  it("reverts an optimistic dismiss and toasts a fallback error on an unshaped failure", async () => {
    mockReview();
    server.use(
      http.post("/api/reservations/admin/purpose-review/:id/dismiss", () =>
        HttpResponse.error(),
      ),
    );
    renderWithProviders(<PurposeReviewPage />);
    await screen.findByText("regression suite");

    const row = screen.getByText("regression suite").closest("li") as HTMLElement;
    fireEvent.click(within(row).getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByText("regression suite")).not.toBeInTheDocument();

    await waitFor(() => expect(screen.getByText("regression suite")).toBeInTheDocument());
    expect(toastError).toHaveBeenCalledWith("Failed to dismiss suggestion");
  });

  it("classifies history and toasts the marked count", async () => {
    mockReview();
    server.use(
      http.post("/api/reservations/admin/purpose/backfill", () =>
        HttpResponse.json({ marked: 7 }),
      ),
    );
    renderWithProviders(<PurposeReviewPage />);
    await screen.findByText("regression suite");

    fireEvent.click(screen.getByRole("button", { name: "Classify history" }));

    await waitFor(() =>
      expect(toastSuccess).toHaveBeenCalledWith("Marked 7 reservations for classification"),
    );
  });

  it("renders pagination controls once total exceeds the page size", async () => {
    mockReview([ITEM_1, ITEM_2], 45);
    renderWithProviders(<PurposeReviewPage />);
    await screen.findByText("regression suite");
    expect(screen.getByText(/Showing 1-20 of 45/)).toBeInTheDocument();
  });
});
