import { http, HttpResponse } from "msw";
import { fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { server } from "../mocks/server";
import { AppLayout } from "@/components/layout/AppLayout";
import { ErrorBoundary } from "@/components/ui/ErrorBoundary";
import { useAuthStore } from "@/stores/authStore";
import { usePreferencesStore } from "@/stores/preferencesStore";

// Issue #1344: a page that throws while rendering is contained to the page.
// The header and navigation stay, the fallback is fixed text (never the
// thrown message), and navigating to another route renders it normally.

const RAW = "secret internal detail from a broken page";

function BrokenPage(): never {
  throw new Error(RAW);
}

function renderApp(initialEntry: string) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[initialEntry]}>
        {/* The same nesting as App.tsx: the root boundary is the last resort. */}
        <ErrorBoundary>
          <Routes>
            <Route element={<AppLayout />}>
              <Route path="/inventory" element={<BrokenPage />} />
              <Route path="/templates" element={<div>templates page content</div>} />
              <Route path="/topology" element={<div>topology page content</div>} />
            </Route>
          </Routes>
        </ErrorBoundary>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

let consoleError: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  // React and the boundary both log the caught error; keep the run quiet.
  consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
  useAuthStore.setState({ accessToken: "t", refreshToken: "r", user: null, isAuthenticated: true });
  usePreferencesStore.setState({ savedFilters: {}, pageSizes: {}, extras: {}, loaded: false });
  server.use(
    http.get("/api/auth/me", () =>
      HttpResponse.json({
        id: "u-1",
        email: "a@b.com",
        username: "alice",
        full_name: "alice",
        contact_email: null,
        phone: null,
        is_active: true,
        role: "user",
        created_at: "2026-01-01T00:00:00Z",
      }),
    ),
    http.get("/api/notifications/notifications/unread-count", () =>
      HttpResponse.json({ count: 0 }),
    ),
    http.get("/api/notifications/notifications", () => HttpResponse.json({ items: [], total: 0 })),
    http.get("/api/user-profile/preferences", () =>
      HttpResponse.json({ saved_filters: {}, page_sizes: {}, extras: {} }),
    ),
  );
});

afterEach(() => {
  consoleError.mockRestore();
});

describe("RouteErrorBoundary in AppLayout (issue #1344)", () => {
  it("keeps the header and navigation and shows the fixed fallback", async () => {
    renderApp("/inventory");

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("This page could not be displayed");
    expect(screen.getByRole("button", { name: "Reload page" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Go to Topology" })).toHaveAttribute("href", "/topology");
    // The layout survived: the nav links and the signed-in user are there.
    expect(await screen.findByText("alice")).toBeInTheDocument();
    for (const label of ["Inventory", "Templates", "Topology", "Reservations"]) {
      expect(screen.getByRole("link", { name: label })).toBeInTheDocument();
    }
    // The root boundary did not trip.
    expect(screen.queryByText("Something went wrong")).not.toBeInTheDocument();
  });

  it("never shows the thrown error's message", async () => {
    renderApp("/inventory");
    await screen.findByRole("alert");
    expect(screen.queryByText(new RegExp(RAW))).not.toBeInTheDocument();
    expect(document.body.textContent).not.toContain(RAW);
  });

  it("recovers when the user navigates to another route", async () => {
    renderApp("/inventory");
    await screen.findByRole("alert");

    fireEvent.click(screen.getByRole("link", { name: "Templates" }));
    expect(await screen.findByText("templates page content")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("recovers through the fallback's own Go to Topology link", async () => {
    renderApp("/inventory");
    await screen.findByRole("alert");

    fireEvent.click(screen.getByRole("link", { name: "Go to Topology" }));
    expect(await screen.findByText("topology page content")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("shows the fallback again when the user returns to the broken route", async () => {
    renderApp("/inventory");
    await screen.findByRole("alert");
    fireEvent.click(screen.getByRole("link", { name: "Templates" }));
    await screen.findByText("templates page content");

    fireEvent.click(screen.getByRole("link", { name: "Inventory" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("This page could not be displayed");
  });
});
