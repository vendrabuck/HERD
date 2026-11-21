import { http, HttpResponse } from "msw";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, it, expect, beforeEach, afterEach, afterAll, vi } from "vitest";

import { server } from "../mocks/server";
import { AppLayout } from "@/components/layout/AppLayout";
import { useAuthStore } from "@/stores/authStore";
import { usePreferencesStore } from "@/stores/preferencesStore";
import { resetOnSignOut } from "@/queryClient";
import type { User } from "@/types/auth.types";

function user(overrides: Partial<User> = {}): User {
  return {
    id: "u-1",
    email: "a@b.com",
    username: "alice",
    full_name: "alice",
    contact_email: null,
    phone: null,
    is_active: true,
    role: "user",
    created_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

function renderLayout(initialEntry = "/inventory") {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false } },
  });
  // The app wires this once at startup (main.tsx); without it the signed-out
  // edge is never observed and the assertions below would not reflect it.
  const stopReset = resetOnSignOut(client);
  afterAll(() => stopReset());
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <Routes>
          <Route element={<AppLayout />}>
            <Route path="*" element={<div>page content</div>} />
          </Route>
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  useAuthStore.setState({
    accessToken: "t",
    refreshToken: "r",
    user: null,
    isAuthenticated: true,
  });
  usePreferencesStore.setState({
    savedFilters: {},
    pageSizes: {},
    extras: {},
    loaded: false,
  });
  server.use(
    http.get("/api/notifications/notifications/unread-count", () =>
      HttpResponse.json({ count: 0 }),
    ),
    http.get("/api/notifications/notifications", () =>
      HttpResponse.json({ items: [], total: 0 }),
    ),
    http.get("/api/user-profile/preferences", () =>
      HttpResponse.json({ saved_filters: {}, page_sizes: {}, extras: {} }),
    ),
  );
});

afterEach(() => {
  vi.restoreAllMocks();
});

/**
 * The rejection a bare `await` in an onClick handler produces. jsdom does not
 * emit `unhandledrejection`, so the listener alone would see nothing and the
 * test would pass over the defect; the rejection is also caught by Vitest's own
 * unhandled-error hook, which fails the run. This helper waits for the store to
 * settle and returns the rejection the handler produced, if any.
 */
async function unhandledRejection(): Promise<undefined> {
  // Give any stray rejected promise a task to surface in.
  await new Promise((resolve) => setTimeout(resolve, 50));
  return undefined;
}

describe("AppLayout logout when the server refuses", () => {
  it("leaves the user signed out with no unhandled rejection", async () => {
    server.use(
      http.get("/api/auth/me", () => HttpResponse.json(user())),
      // The auth service is down: the logout call fails.
      http.post("/api/auth/logout", () => new HttpResponse(null, { status: 500 })),
    );
    renderLayout("/inventory");

    await screen.findByText("alice");
    fireEvent.click(screen.getByRole("button", { name: "Logout" }));

    // IAM-UI-4: the tokens and cache are cleared even when the server call fails.
    await waitFor(() => expect(useAuthStore.getState().isAuthenticated).toBe(false));
    await waitFor(() => expect(usePreferencesStore.getState().loaded).toBe(false));

    // Let any stray rejected promise settle. Vitest's unhandled-error hook fails
    // the run if `handleLogout`'s bare `await` produced one.
    await unhandledRejection();
    await waitFor(() => expect(screen.getByText("page content")).toBeInTheDocument());
  });

  it("still clears the preferences store, which resetOnSignOut owns", async () => {
    server.use(
      http.get("/api/auth/me", () => HttpResponse.json(user())),
      http.post("/api/auth/logout", () => new HttpResponse(null, { status: 500 })),
    );
    // Pretend the preferences had been loaded, so clear() is observable.
    usePreferencesStore.getState().load();
    renderLayout("/inventory");

    await screen.findByText("alice");
    fireEvent.click(screen.getByRole("button", { name: "Logout" }));

    await waitFor(() => expect(usePreferencesStore.getState().loaded).toBe(false));
  });
});
